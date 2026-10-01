# =============================================================================
# 按 **接口** 与 **需求/验收项** 两个维度统计「跑通一次实际要花多少时间」。
#
# 为什么要有它：docs/09 里只有**阶段级**秒数（M1 43s / M5 167s ...）。
# 阶段级回答不了两个问题：
#   1) 哪条**接口**真的慢？（同一段里既有 1 条纯查询也有 1 次模型调用）
#   2) 哪条**需求**最贵？（一个 AC 可能横跨多条接口与多个阶段）
#
# 数据源（两个都是既有的，不需要改产品代码）：
#   A. 网关结构化日志 `msg=http.request ... method=.. route=.. status=.. elapsed_ms=..`
#      -> 口径「网关自报服务端耗时」，**含**同步等待 AI 的时间，**不含**客户端开销。
#   B. 阶段落盘输出 `$env:TEMP\stage*-final.txt`
#      -> 口径「客户端墙钟」，含 IWR/curl 的固定开销与脚本自身逻辑。
#
# A 与 B 之差本身就是一条结论（见 docs/09 §3.4：`GET /conversations`
# 网关自报中位 5.2ms / 脚本侧 20.9ms，差值是客户端与机器负载）。
#
# **两个口径的精度不同，报出时必须分开写**：
#   * 接口维度（来自 A）—— 可复算，误差小。
#   * 需求维度（断言数 x 阶段单价）—— **是分摊估算**，只在"哪条需求最贵"的量级上有意义，
#     不能当成"这条需求精确耗时 N 秒"。脚本会显式标注 [EST]。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\cost_report.ps1
#   powershell -ExecutionPolicy Bypass -File tools\cost_report.ps1 -Top 20
#   powershell -ExecutionPolicy Bypass -File tools\cost_report.ps1 -Logs "gw-allstages-A.log"
# 输出（UTF-8）默认写到 $env:TEMP\cost-report.txt，同时打印到终端。
# =============================================================================

[CmdletBinding()]
param(
    # 要分析的网关日志；默认取 $env:TEMP 下所有 gw-*.log（含各阶段自拉的临时实例）。
    [string[]]$Logs,
    # 阶段输出文件；默认取 $env:TEMP\stage*-final.txt。
    [string[]]$StageFiles,
    # 接口表只显示总耗时最高的前 N 条。
    [int]$Top = 25,
    [string]$Out
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not $Out) { $Out = Join-Path $env:TEMP 'cost-report.txt' }

# 日志文件 -> 它属于哪一段（决定要不要把它的数字算进"阶段总耗时"）。
# 全阶段套件自拉 A/B 两个实例；stage5/6 另外自拉临时实例，要分开看：
# 临时实例是**故意配成失败**的（空闲上游端口 / 1MB 上限 / free 档 1 次配额），
# 把它们和正常实例混在一起聚合会让"接口耗时"失去意义。
function Get-LogOwner {
    param([string]$Name)
    switch -Regex ($Name) {
        'gw-allstages-A' { return 'A' }        # M1-M4 / M6
        'gw-allstages-B' { return 'B' }        # M5 主网关（生产默认档）
        'gw-stage5' { return 'aux' }           # M5 的辅助实例（小上限/配额/降级）
        'gw-stage6' { return 'aux' }           # M6 干净实例（业务断言都打它）
        'gw-degraded' { return 'aux' }
        'gw-main2' { return 'manual' }         # 手工起的实例，只做参考
        'gw-main' { return 'manual' }
        default { return 'other' }
    }
}

if (-not $Logs -or $Logs.Count -eq 0) {
    $Logs = @(Get-ChildItem $env:TEMP -Filter 'gw-*.log' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime | ForEach-Object { $_.FullName })
}
$Logs = @($Logs | Where-Object { Test-Path $_ })

if (-not $StageFiles -or $StageFiles.Count -eq 0) {
    $StageFiles = @(Get-ChildItem $env:TEMP -Filter 'stage*-final.txt' -File -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
}
$StageFiles = @($StageFiles | Where-Object { Test-Path $_ })

$sb = New-Object System.Text.StringBuilder
function Emit {
    param([string]$Text = "")
    [void]$sb.AppendLine($Text)
    Write-Host $Text
}

# ---- 分位数：对已排序数组取「最近秩」而不是插值 ------------------------------
# 插值会给出样本里不存在的值，让 "P95=87.3ms" 看起来比实际精确。
function Get-Quantile {
    param([double[]]$Sorted, [double]$Q)
    if ($Sorted.Count -eq 0) { return [double]0 }
    $idx = [int][Math]::Ceiling($Q * $Sorted.Count) - 1
    if ($idx -lt 0) { $idx = 0 }
    if ($idx -ge $Sorted.Count) { $idx = $Sorted.Count - 1 }
    return $Sorted[$idx]
}

# =============================================================================
# 一、解析网关日志 -> 按 (owner, method, route) 聚合
# =============================================================================
$agg = @{}
$logLineTotal = 0
$parsed = 0

foreach ($path in $Logs) {
    $name = [IO.Path]::GetFileName($path)
    $owner = Get-LogOwner -Name $name
    foreach ($line in (Get-Content $path -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $logLineTotal++
        if ($line -notmatch 'msg=http\.request') { continue }
        $method = ''; $route = ''; $status = ''; $ms = $null
        if ($line -match 'method=(\S+)') { $method = $Matches[1] }
        # route 一般是裸值，但 slog 在值含特殊字符时会加引号 -> 两种都收。
        if ($line -match 'route=("[^"]*"|\S+)') { $route = $Matches[1].Trim('"') }
        if ($line -match 'status=(\d+)') { $status = $Matches[1] }
        if ($line -match 'elapsed_ms=([0-9.]+)') { $ms = [double]$Matches[1] }
        if (-not $route -or $null -eq $ms) { continue }
        $parsed++
        $key = "$owner|$method $route"
        if (-not $agg.ContainsKey($key)) {
            $agg[$key] = @{
                Owner = $owner; Method = $method; Route = $route
                Ms = New-Object System.Collections.ArrayList
                Status = @{}
            }
        }
        [void]$agg[$key].Ms.Add($ms)
        $s = $agg[$key].Status
        if (-not $s.ContainsKey($status)) { $s[$status] = 0 }
        $s[$status]++
    }
}

Emit "================ 接口维度（网关自报 elapsed_ms）================"
Emit ""
Emit ("日志 {0} 个文件 / {1} 行，其中 http.request 摘要 {2} 条" -f $Logs.Count, $logLineTotal, $parsed)
Emit ""
Emit "口径说明：elapsed_ms 是**网关服务端处理时间**，会包含同步等待 AI 的耗时；"
Emit "          不含客户端（IWR/curl）开销与网络往返。status=0 表示客户端主动断开。"
Emit "          Owner: A=M1-M4/M6 实例，B=M5 生产默认实例，aux=故意配成失败的临时实例。"
Emit ""

$rows = @()
foreach ($k in $agg.Keys) {
    $e = $agg[$k]
    $sorted = @($e.Ms.ToArray() | Sort-Object)
    $sum = ($e.Ms | Measure-Object -Sum).Sum
    $fail = 0
    foreach ($st in $e.Status.Keys) {
        if ($st -ne '200' -and $st -ne '201' -and $st -ne '202' -and $st -ne '204') { $fail += $e.Status[$st] }
    }
    $rows += [pscustomobject]@{
        Owner   = $e.Owner
        Method  = $e.Method
        Route   = $e.Route
        N       = $e.Ms.Count
        TotalMs = [Math]::Round($sum, 1)
        Median  = [Math]::Round((Get-Quantile -Sorted $sorted -Q 0.5), 2)
        P95     = [Math]::Round((Get-Quantile -Sorted $sorted -Q 0.95), 2)
        Max     = [Math]::Round($sorted[$sorted.Count - 1], 2)
        Min     = [Math]::Round($sorted[0], 2)
        Non2xx  = $fail
    }
}

$rows = @($rows | Sort-Object -Property TotalMs -Descending)

Emit "---- 按**总耗时**排序（谁把阶段时间吃掉最多）----"
Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,11} {5,9} {6,9} {7,9} {8,6}" -f `
        'Owner', 'Method', 'Route', 'N', 'TotalMs', 'Median', 'P95', 'Max', '!2xx')
Emit ("-" * 120)
foreach ($r in ($rows | Where-Object { $_.Owner -eq 'A' -or $_.Owner -eq 'B' } | Select-Object -First $Top)) {
    Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,11:N1} {5,9:N2} {6,9:N2} {7,9:N2} {8,6}" -f `
            $r.Owner, $r.Method, $r.Route, $r.N, $r.TotalMs, $r.Median, $r.P95, $r.Max, $r.Non2xx)
}
Emit ""
Emit "---- 同上，但含 aux / manual / other（临时实例的数字**故意是失败的**，别当产品耗时）----"
Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,11} {5,9} {6,9} {7,9} {8,6}" -f `
        'Owner', 'Method', 'Route', 'N', 'TotalMs', 'Median', 'P95', 'Max', '!2xx')
Emit ("-" * 120)
foreach ($r in ($rows | Select-Object -First $Top)) {
    Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,11:N1} {5,9:N2} {6,9:N2} {7,9:N2} {8,6}" -f `
            $r.Owner, $r.Method, $r.Route, $r.N, $r.TotalMs, $r.Median, $r.P95, $r.Max, $r.Non2xx)
}
Emit ""

Emit "---- 按**单次中位耗时**排序（谁一条就贵，与调用次数无关）----"
Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,9} {5,11} {6,6}" -f `
        'Owner', 'Method', 'Route', 'N', 'Median', 'TotalMs', '!2xx')
Emit ("-" * 100)
foreach ($r in ($rows | Where-Object { $_.N -ge 3 } | Sort-Object -Property Median -Descending | Select-Object -First $Top)) {
    Emit ("{0,-7} {1,-6} {2,-46} {3,5} {4,9:N2} {5,11:N1} {6,6}" -f `
            $r.Owner, $r.Method, $r.Route, $r.N, $r.Median, $r.TotalMs, $r.Non2xx)
}
Emit ""

# 每个 owner 的合计：能直接和阶段墙钟对比。
Emit "---- 按实例分组的服务端耗时合计（可与阶段墙钟对比，差额=客户端开销）----"
Emit ("{0,-8} {1,9} {2,14} {3,14}" -f 'Owner', 'Requests', 'ServerTotalMs', 'ServerTotalSec')
Emit ("-" * 48)
foreach ($g in ($rows | Group-Object Owner | Sort-Object Name)) {
    $n = ($g.Group | Measure-Object -Property N -Sum).Sum
    $t = ($g.Group | Measure-Object -Property TotalMs -Sum).Sum
    Emit ("{0,-8} {1,9} {2,14:N1} {3,14:N2}" -f $g.Name, $n, $t, ($t / 1000))
}
Emit ""

# =============================================================================
# 二、阶段墙钟 -> 断言计数
# =============================================================================
Emit "================ 阶段维度（来自 stage*-final.txt）================"
Emit ""
Emit "口径说明：墙钟含 IWR/curl 固定开销与脚本自身逻辑；PASS/FAIL/SKIP 按行首标记统计。"
Emit ""

$stageRows = @()
foreach ($f in ($StageFiles | Sort-Object)) {
    $text = Get-Content $f -Encoding UTF8 -Raw
    $pass = ([regex]::Matches($text, '(?m)^\s*(\[PASS\]|PASS)\s')).Count
    $fail = ([regex]::Matches($text, '(?m)^\s*(\[FAIL\]|FAIL)\s')).Count
    $skip = ([regex]::Matches($text, '(?m)^\s*(\[SKIP\]|SKIP)\s')).Count
    $stageRows += [pscustomobject]@{
        Stage = [IO.Path]::GetFileNameWithoutExtension($f) -replace '-final$', ''
        Pass  = $pass; Fail = $fail; Skip = $skip; File = $f
    }
}

Emit ("{0,-8} {1,6} {2,6} {3,6}" -f 'Stage', 'PASS', 'FAIL', 'SKIP')
Emit ("-" * 30)
foreach ($r in $stageRows) {
    Emit ("{0,-8} {1,6} {2,6} {3,6}" -f $r.Stage, $r.Pass, $r.Fail, $r.Skip)
}
Emit ""
Emit "注：阶段**秒数**不写在这里（它是 run_all_stages.ps1 运行时测的，不在落盘文件里）。"
Emit "    跑全套时它打印的汇总表就是权威值：M1 43 / M2 79 / M3 8 / M4 42 / M5 167 / M6 91。"
Emit ""

# =============================================================================
# 三、需求/验收项维度（估算，必须标明是分摊）
# =============================================================================
Emit "================ 需求维度 [EST 分摊估算] ================"
Emit ""
Emit "方法：从断言名里抽 AC-*/REQ-* 编号 -> 该编号的断言数 x 所在阶段的每断言单价。"
Emit "局限：**这是分摊，不是测量**。一条断言可能触发多次接口调用，也可能只查内存。"
Emit "      它只在"哪条需求最贵"的量级上有意义，别当成精确耗时。"
Emit "      另：目前只有极少数断言名带编号，覆盖率很低（见文件末尾的百分比）。"
Emit "更硬的对应关系在上一节的「接口 -> 次数 -> 耗时」表里（那部分是可复算的）。"
Emit ""

# 阶段墙钟（来自 run_all_stages.ps1 的汇总表，人工同步一次即可；跑完全套请核对）
$stageSeconds = @{ 'stage1' = 43; 'stage2' = 79; 'stage3' = 8; 'stage4' = 42; 'stage5' = 167; 'stage6' = 91 }

$acAgg = @{}
foreach ($r in $stageRows) {
    if (-not $stageSeconds.ContainsKey($r.Stage)) { continue }
    $sec = [double]$stageSeconds[$r.Stage]
    $unit = if ($r.Pass -gt 0) { $sec / $r.Pass } else { 0 }
    $text = Get-Content $r.File -Encoding UTF8 -Raw
    # 认 (AC-CONV-07) 这种括号写法，也认裸写 AC-CONV-07
    foreach ($m in [regex]::Matches($text, '(?m)^\s*(\[?PASS\]?|PASS)\s.*?((AC|REQ)-[A-Z]+-\d+)')) {
        $id = $m.Groups[2].Value
        if (-not $acAgg.ContainsKey($id)) {
            $acAgg[$id] = @{ Id = $id; N = 0; Stages = @{}; EstSec = [double]0 }
        }
        $acAgg[$id].N++
        $acAgg[$id].EstSec += $unit
        if (-not $acAgg[$id].Stages.ContainsKey($r.Stage)) { $acAgg[$id].Stages[$r.Stage] = 0 }
        $acAgg[$id].Stages[$r.Stage]++
    }
}

$wallTotal = ($stageSeconds.Values | Measure-Object -Sum).Sum
if ($acAgg.Count -eq 0) {
    Emit "（断言名里没有 AC-/REQ- 编号，或阶段文件为空 -> 跳过这一节）"
}
else {
    Emit ("{0,-18} {1,8} {2,11} {3,28}" -f 'Req/Ac', 'Asserts', 'EstSec', 'Stages')
    Emit ("-" * 68)
    foreach ($k in ($acAgg.Keys | Sort-Object { -$acAgg[$_].EstSec })) {
        $e = $acAgg[$k]
        $st = (($e.Stages.Keys | Sort-Object) | ForEach-Object { "$_ x$($e.Stages[$_])" }) -join ', '
        Emit ("{0,-18} {1,8} {2,11:N1} {3,28}" -f $e.Id, $e.N, $e.EstSec, $st)
    }
    Emit ""
    $sumEst = ($acAgg.Keys | ForEach-Object { $acAgg[$_].EstSec } | Measure-Object -Sum).Sum
    Emit ("有编号的断言合计估算 {0:N1}s（占全部阶段墙钟 {1}s 的 {2:N1}%）—— 其余断言没标编号。" -f `
            $sumEst, $wallTotal, (100 * $sumEst / $wallTotal))
}

Emit ""
Emit "================ 原始数据 ================"
Emit "日志："
foreach ($l in $Logs) { Emit ("  " + $l) }
Emit "阶段输出："
foreach ($f in $StageFiles) { Emit ("  " + $f) }
Emit ""
Emit ("报告文件：" + $Out)

[IO.File]::WriteAllText($Out, $sb.ToString(), [Text.UTF8Encoding]::new($false))
Write-Host ""
Write-Host ("已写入 " + $Out) -ForegroundColor DarkGray
exit 0
