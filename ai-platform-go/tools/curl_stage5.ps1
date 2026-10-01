# M5 验收：配额预扣/回滚、多维限流、熔断降级、上传流式转发
#
# 前置：
#   1) AI gRPC（上传与配额的成功路径要多一次真实编排）：
#        $env:GRPC_ENABLED="true"; $env:GRPC_HOST="127.0.0.1"; $env:GRPC_PORT="50051"
#        uv run python -m app.grpc
#   2) AI HTTP（上传是 **HTTP 透传**，走 8000）：
#        $env:METRICS_PORT="9106"; uv run python -m app.main
#   3) 主网关（18080，与 stage1..4 同一套变量）
#
# 本脚本会**自己拉两个临时网关实例**（用 18082/18083 与 9109/9110，避开主环境），
# 因为 M5 的三条验收各自需要「被改坏的环境」：
#   - 配额耗尽：必须把 free 档的 chat_requests 调到 1（QUOTA_PLAN_MAP）
#   - AI 不可达 / 熔断：必须把上游指向一个**必然连不上**的端口
# 直接在主网关上做这两件事，会让同一台机器上的其它验收脚本失去可比基线。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage5.ps1
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage5.ps1 -SkipQuota
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage5.ps1 -SkipAiDown
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage5.ps1 -UploadMB 8   # 快速档
#
# `-UploadMB` 是**字节**口径（默认 8）：不要为了「更像 AC 原文」而调大，
# 详见该参数的注释（49MB 会把 AI 侧的事件循环拖死十几分钟）。

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:18080",
    # 配额实例：小配额，只用来验「第 2 次被拦 + 没打给 AI」。
    [int]$QuotaPort = 18082,
    [int]$QuotaMetricsPort = 9109,
    # 降级实例：上游指向死端口。
    [int]$DeadPort = 18083,
    [int]$DeadMetricsPort = 9110,
    # 上传体积（**MB，按 UTF-8 字节**）。
    #
    # ⚠️ 默认 8 而不是 49（曾用 49，2026-09-30 改成 8）：这个值不是「越大越好」。
    #
    # AC-ORCH-05 要证的是「网关没有把正文整读进内存」，判据有两条：
    #   ① 绝对：RSS 峰值增量 < 50MB；
    #   ② 相对：RSS 峰值增量 < 文件体积。
    # 真正有分辨力的是②——流式实现读 8MB 与读 49MB 的增量都是 0.1~0.2MB，
    # 而非流式实现是 +文件体积，8MB 时余量仍有 40 倍。所以把文件做大只是
    # 让②的分母变大，**并不能**让判据更严，却能真实地把 AI 侧拖死：
    #
    #   实测 49MB → AI 侧切出 40,475 片（被 `MAX_DOC_CHUNKS=10000` 截断），
    #   随后在 `INFRA_BACKEND=memory` + `EMBEDDING_DEVICE=cpu` 下于事件循环里
    #   跑 CPU 向量化，**`/health` 从此不再应答（超过 10 分钟）**，
    #   于是紧跟其后的 stage6 会以「连不上 AI」大面积失败 ——
    #   现象看起来像网关坏了，实际是上一条脚本把 AI 喂爆了。
    #
    # 顺带记录一个上限事实：`UPLOAD_MAX_MB=50` 时**不能**传 `-UploadMB 50`，
    # multipart 头尾还会多出约 200 字节，必然被网关自己的 413 拒掉
    # （而 413 是另一条断言，用 `UPLOAD_MAX_MB=1` 的临时实例验，不靠这里）。
    [int]$UploadMB = 8,
    # 熔断阈值：与网关默认 CB_FAILURE_THRESHOLD 一致。
    [int]$CBFailures = 10,
    [switch]$SkipUpload,
    [switch]$SkipQuota,
    [switch]$SkipAiDown
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[System.Net.ServicePointManager]::Expect100Continue = $false

$script:Total = 0
$script:Failed = 0
$script:Failures = New-Object System.Collections.ArrayList
$script:Aux = New-Object System.Collections.ArrayList

# RSS 采样要采的是**被验的那个实例**，端口只能从 `-BaseUrl` 派生。
#
# 曾经在这里写死 18080：单跑（默认 BaseUrl）时一切正常，一旦被
# `run_all_stages.ps1` 以 `-BaseUrl http://127.0.0.1:18086` 调用，
# 采样就拿不到进程，AC-ORCH-05 的两条判据**静默降级成 SKIP**。
# 症状极具迷惑性：汇总只显示「M5 少了 2 条 PASS、多了 1 条 SKIP」，
# 看起来像断言数波动，实际是「判据根本没跑」。
$MainPort = if ($BaseUrl -match ':(\d+)') { [int]$Matches[1] } else { 18080 }

function Assert-True {
    param([string]$Name, [bool]$Condition, [string]$Detail = "")
    $script:Total++
    if ($Condition) {
        Write-Host ("  PASS  " + $Name) -ForegroundColor DarkGreen
    } else {
        $script:Failed++
        [void]$script:Failures.Add($Name + " :: " + $Detail)
        Write-Host ("  FAIL  " + $Name + "  " + $Detail) -ForegroundColor Red
    }
}

function Assert-Info {
    param([string]$Name, [string]$Detail)
    Write-Host ("  INFO  " + $Name + "  " + $Detail) -ForegroundColor DarkCyan
}

function Assert-Skip {
    param([string]$Name, [string]$Detail)
    Write-Host ("  SKIP  " + $Name + "  " + $Detail) -ForegroundColor DarkYellow
}

# ---- 压掉脚本自身的地板：关掉系统代理 ----
# 本机常驻代理（Clash 一类，127.0.0.1:7897）时，PS 5.1 的 `Invoke-WebRequest` 连
# **回环地址**也会走代理解析：实测同一个 `/health`，IWR 走代理 10s 超时，而
# `curl.exe --noproxy '*'` 只要 412ms（run_all_stages.ps1 头部 / docs/09-§2.1）。
# 本脚本 100% 打 127.0.0.1（主网关 + 两个辅助实例的 `/health/live` 与 `/metrics`），
# 关掉代理是纯收益：省掉的是**每次往返**的固定开销。
#
# 为什么设 .NET 静态属性而不只写 `-Proxy $null`：PS 5.1 的 `-Proxy` 落在
# `if (Proxy != null) { request.Proxy = Proxy }` 上，传 `$null` 在部分版本上等价于
# **不传**（代理照旧生效）。进程级 `DefaultWebProxy = $null` 才是确定生效的那一下 ——
# 它同时覆盖 `Invoke-Api`、下面的 `/health/live` 就绪探测与 `/metrics` 抓取；
# `Invoke-Api` 的参数表里另外带上 `-Proxy $null`（PS 6+ 用 `-NoProxy`）做双保险。
#
# ⚠ 只在「目标全是回环」时成立：把 -BaseUrl 指到非回环地址请删掉这一段。
[System.Net.WebRequest]::DefaultWebProxy = $null

# ---- JSON 信封接口（与 stage1..4 同一份实现）----
function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Token,
        [object]$Body,
        [string]$TraceId = "",
        [string]$ExtraHeaderName = "",
        [string]$ExtraHeaderValue = "",
        [string]$Base = $BaseUrl,
        # 默认 120s 是为真实 AI 回答留的余量（一次 RAG + 长回答可能几十秒）。
        # 但**拒绝路径**（熔断打开、配额不足）必须秒回 —— 给它们单独传一个小值，
        # 否则「本来该立刻返回的请求被阻塞」会表现为「脚本跑了 2 分钟才报 status=0」，
        # 而 status=0 + 空错误码恰好也是超时以外的很多问题的现象（比如并发死锁），
        # 说不清是哪一个。小超时让 120s 级别的挂起直接变成一条明确的失败。
        [int]$TimeoutSec = 120
    )
    $headers = @{ "X-Request-Id" = "req_stage5" }
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }
    if ($TraceId) { $headers["traceparent"] = "00-$TraceId-7f3a91c2d5b6480e-01" }
    if ($ExtraHeaderName) { $headers[$ExtraHeaderName] = $ExtraHeaderValue }
    $params = @{ Method = $Method; Uri = ($Base + $Path); Headers = $headers; UseBasicParsing = $true; TimeoutSec = $TimeoutSec }
    if ($null -ne $Body) {
        $params["ContentType"] = "application/json"
        $json = $Body | ConvertTo-Json -Depth 8 -Compress
        $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    # 与文件顶部 `DefaultWebProxy = $null` 配对的双保险：PS 6+ 用 `-NoProxy`，
    # PS 5.1 没有这个开关，只能传 `-Proxy $null`（顶部那段注释解释了为什么它单独用不够）。
    if ($PSVersionTable.PSVersion.Major -ge 6) { $params["NoProxy"] = $true } else { $params["Proxy"] = $null }

    $status = 0
    $text = ""
    $respHeaders = $null
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest @params
        $status = [int]$resp.StatusCode
        $text = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        $respHeaders = $resp.Headers
    } catch {
        $r = $_.Exception.Response
        if ($r) {
            $status = [int]$r.StatusCode
            $respHeaders = $r.Headers
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $text = $_.ErrorDetails.Message
            } else {
                $sr = New-Object System.IO.StreamReader($r.GetResponseStream(), [System.Text.Encoding]::UTF8)
                $text = $sr.ReadToEnd()
            }
        } else {
            $text = $_.Exception.Message
        }
    }
    $watch.Stop()
    $obj = $null
    if ($text) { try { $obj = $text | ConvertFrom-Json } catch { $obj = $null } }
    $traceOut = ""
    if ($respHeaders -and $respHeaders["X-Trace-Id"]) { $traceOut = [string]$respHeaders["X-Trace-Id"] }
    return [pscustomobject]@{
        Status = $status; Text = $text; Json = $obj; Trace = $traceOut
        Ms = $watch.Elapsed.TotalMilliseconds
    }
}

# ---- 辅助网关实例 ----
#
# PS 5.1 的 `Start-Process` 没有 `-Environment`，于是只能在**当前**进程里设 env
# 再启动（子进程继承）。这要求每次启动前把**全部**变量重设一遍，
# 而不是「只设差异项」—— 否则上一次残留的 QUOTA_PLAN_MAP / 死端口会串到下一个实例。
function Start-AuxGateway {
    param(
        [string]$Exe,
        [int]$Port,
        [int]$MetricsPort,
        [string]$LogPath,
        [hashtable]$ExtraEnv = @{}
    )
    $env:HTTP_ADDR = "127.0.0.1:$Port"
    $env:APP_ENV = "local"
    $env:DEBUG = "false"
    $env:LOG_LEVEL = "info"
    # ⚠️ `LOG_FORMAT` 必须显式重设，不能依赖「本进程没设过」。
    # 这是与 `ai-platform`（Python）**同名不同域**的配置项：
    # 网关只接受 `json` / `text`，而 AI 侧只接受 `json` / `console`。
    # 在同一个终端里先后跑两侧（或先起 AI 再跑本脚本）时，
    # `LOG_FORMAT=console` 会残留下来，本函数不重设它的话子进程
    # 会直接**启动失败**：`LOG_FORMAT="console" 不合法`，
    # 而现象只是「实例没起来」，看不出是环境变量串了项目。
    $env:LOG_FORMAT = "text"
    $env:METRICS_ENABLED = "true"
    $env:METRICS_PORT = "$MetricsPort"
    $env:METRICS_ALLOW_CIDRS = "127.0.0.1/32"
    $env:AI_GRPC_ENABLED = "true"
    $env:AI_PLATFORM_GRPC_TARGET = "127.0.0.1:50051"
    $env:AI_PLATFORM_BASE_URL = "http://127.0.0.1:8000"
    $env:OTEL_ENABLED = "false"
    # ⚠️ 限流档位也要显式重设，理由同 `LOG_FORMAT`（「只设差异项」会串值）。
    # 这里的默认值刻意与网关的**出厂默认**一致：本脚本第 5 节要断言
    # 「连续错误登录会出现 429」，而它跑在 `$BaseUrl`（主网关）上 ——
    # 若把辅助实例放宽到 1000，那些实例倒是没事，但**当调用者用宽松环境跑本脚本时**
    # （例如 `run_all_stages.ps1` 给 M1–M4 用了放宽限流的实例），
    # 残留值会让「第 25 次都没限流」，断言失败且看不出原因。
    # 所以只有一个规矩：**每个实例启动前重设全部相关变量**。
    if (-not $ExtraEnv.ContainsKey("LOGIN_RATE_PER_MINUTE")) { $env:LOGIN_RATE_PER_MINUTE = "10" }
    if (-not $ExtraEnv.ContainsKey("LOGIN_ACCOUNT_RATE_PER_HOUR")) { $env:LOGIN_ACCOUNT_RATE_PER_HOUR = "20" }
    # 清掉可能残留在本进程里的键（ExtraEnv 里没有的）。
    foreach ($k in @("QUOTA_PLAN_MAP", "UPLOAD_MAX_MB", "CB_FAILURE_THRESHOLD", "CB_OPEN_SECONDS")) {
        if (-not $ExtraEnv.ContainsKey($k)) { Remove-Item ("env:" + $k) -ErrorAction SilentlyContinue }
    }
    foreach ($k in $ExtraEnv.Keys) { Set-Item ("env:" + $k) $ExtraEnv[$k] }

    # ⚠️ 必须 `-WindowStyle Hidden`（= 独立控制台），**不能用 `-NoNewWindow`**。
    # 后者让网关和本脚本**共用调用者的控制台**，于是「控制台收到关闭事件」
    # （关掉终端标签页 / 宿主把终端回收 / 在同一个终端里按 Ctrl+C）
    # 会连带杀掉这些实例。实测过后果：辅助实例在 `signal=shutdown` 后立刻退出，
    # 紧接着的 `register` 拿到 `503 ... cause="context canceled"`，
    # 而脚本正在等它 → **整棵树跟着终端一起消失**，只留下一行没结果的标题
    # （现场只能从实例日志里的 `app.signal_received signal=shutdown` 反推）。
    # `-WindowStyle Hidden` 让每个实例有自己的（隐藏）控制台，从根上免疫这件事；
    # 与 `run_all_stages.ps1` / `curl_stage6.ps1` 保持一致。
    $p = Start-Process -FilePath $Exe -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $LogPath -RedirectStandardError ($LogPath + ".err")
    [void]$script:Aux.Add($p)

    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        if ($p.HasExited) {
            # 启动失败时把 stderr 尾部打出来。原先只报「见 <日志文件>」，
            # 而真正的原因（配置校验失败 / 端口占用）只在 .err 里 ——
            # 排障时得手动去翻文件，且很容易误以为是端口冲突。
            Show-AuxFailure -LogPath $LogPath -Port $Port
            return $null
        }
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health/live" -UseBasicParsing -TimeoutSec 1
            return $p
        } catch { Start-Sleep -Milliseconds 300 }
    }
    Show-AuxFailure -LogPath $LogPath -Port $Port
    return $null
}

# Show-AuxFailure 把子进程的 stderr 尾部贴到控制台。
function Show-AuxFailure {
    param([string]$LogPath, [int]$Port)
    foreach ($suffix in @(".err", "")) {
        $f = $LogPath + $suffix
        if (Test-Path $f) {
            $tail = Get-Content $f -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 6
            if ($tail) {
                Write-Host ("      子进程输出（" + (Split-Path $f -Leaf) + "）：") -ForegroundColor DarkGray
                foreach ($l in $tail) { Write-Host ("        " + $l) -ForegroundColor DarkGray }
            }
        }
    }
    Write-Host ("      端口 $Port 是否被占用：") -ForegroundColor DarkGray
    $busy = @(netstat -ano | Select-String ":$Port ") 
    if ($busy.Count -eq 0) { Write-Host "        未占用（说明不是端口冲突）" -ForegroundColor DarkGray }
    else { foreach ($b in $busy) { Write-Host ("        " + $b.Line.Trim()) -ForegroundColor DarkGray } }
}

# ---- 抓指标 ----
function Get-MetricValue {
    param([int]$MetricsPort, [string]$Pattern)
    # `-UseBasicParsing`：PS 5.1 的 Invoke-WebRequest 没有它会弹「脚本执行风险」交互确认。
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$MetricsPort/metrics" -UseBasicParsing -TimeoutSec 5
    foreach ($line in ($r.Content -split "`n")) {
        $t = $line.Trim()
        if ($t -like "#*" -or -not $t) { continue }
        if ($t -match $Pattern) { return [double](($t -split '\s+')[-1]) }
    }
    return 0
}

function Get-SeriesCount {
    param([int]$MetricsPort, [string]$Prefix)
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$MetricsPort/metrics" -UseBasicParsing -TimeoutSec 5
    return @(($r.Content -split "`n") | Where-Object { $_ -match ("^" + [regex]::Escape($Prefix)) }).Count
}

# ---- 上传（multipart 手工拼装）----
#
# `Invoke-WebRequest -Form` 是 PS 6+ 才有的，PS 5.1 只能自己拼 body。
# 用 `HttpWebRequest` 是为了能显式控制 `Content-Length`：
# 网关的前置 413 判断读的就是它。
function New-MultipartBody {
    param([string]$FileName, [byte[]]$Content, [string]$Boundary)
    $crlf = "`r`n"
    $head = "--$Boundary$crlf" +
        "Content-Disposition: form-data; name=`"file`"; filename=`"$FileName`"$crlf" +
        "Content-Type: text/markdown$crlf$crlf"
    $tail = "$crlf--$Boundary--$crlf"
    $hb = [System.Text.Encoding]::UTF8.GetBytes($head)
    $tb = [System.Text.Encoding]::UTF8.GetBytes($tail)

    # ⚠️ 不用 MemoryStream：它会按 2 倍扩容，峰值同时存在「内部缓冲 + 扩容副本 +
    # `ToArray()` 结果」三份。而且 `return $ms.ToArray()` 会被 PowerShell 的管道
    # **展开**成 `Object[]`（每个字节再装箱一次），下一站 `[byte[]]$Body` 又要把它
    # 转回来 —— 一次 8MB 的拼装能吃掉近 GB。
    # 一次分配到位 + `Array::Copy`，峰值就是一份 $total。
    $total = $hb.Length + $Content.Length + $tb.Length
    $out = [byte[]]::new($total)
    [System.Array]::Copy($hb, 0, $out, 0, $hb.Length)
    [System.Array]::Copy($Content, 0, $out, $hb.Length, $Content.Length)
    [System.Array]::Copy($tb, 0, $out, $hb.Length + $Content.Length, $tb.Length)
    return , $out
}

# New-TextBody 造一份**恰好 $TargetBytes 字节**的可读 markdown。
#
# 两条容易踩空的地方，都是实测踩出来的：
#  1. 必须按**字节**计数。中文在 UTF-8 下是 3 字节，早先的写法用
#     `while ($sb.Length -lt $target)`（**字符数**比大小），于是 `-UploadMB 8`
#     实际传出去 **19.19 MB** —— 而脚本打印的是字节数，看起来像打印算错，
#     实际是参数没生效。
#  2. 段号会随段数变长（`-f 0` 1 位、`-f 540844` 6 位），按首行估算的总数会**超出**
#     目标（实测 49MB 估成 51.47MB）。所以估算之后按字节裁到目标以内、在最后一个
#     换行处收尾 —— 判据只有一个：传出去的就是它说的那么大。
#
# ⚠️ 裁剪**必须**用 `Array::Copy`，不能用 `$bytes[0..$cut]`。
# PowerShell 的 range 运算符在 `byte[]` 上返回的是 **`Object[]`**，每个字节都被**装箱**
# （对象头 16 字节 + 指针 8 字节，一个字节涨到 ~32 字节）。实测：
#
#     $raw = 8MB 的 byte[];  $sliced = $raw[0..(8MB-1)]
#     → 类型 Object[]，RSS **+618 MB**（77 倍）
#
# 于是一份 8MB 的 body 要吃掉近 1GB，在「16GB 机器但只剩 1.7GB 可用」的日常状态下
# 就是 `OutOfMemoryException`。换 `Array::Copy` 后同样的 8MB 只花 ~40MB（实测
# RSS 88MB → 127MB）。
function New-TextBody {
    param([int64]$TargetBytes, [string]$Line)
    $lineBytes = [System.Text.Encoding]::UTF8.GetByteCount(($Line -f 0))
    $segments = [int][Math]::Ceiling($TargetBytes / $lineBytes)
    $sb = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $segments; $i++) {
        [void]$sb.Append(($Line -f $i))
    }
    $raw = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
    # 及时断开大对象：StringBuilder 的 UTF-16 缓冲（约 2 字节/字符）与 byte[] 不该同时活着。
    $sb = $null
    if ($raw.Length -gt $TargetBytes) {
        $cut = [int][Math]::Min($TargetBytes, $raw.Length - 1)
        while ($cut -gt 0 -and $raw[$cut] -ne 10) { $cut-- }
        $len = $cut + 1
        $out = [byte[]]::new($len)
        [System.Array]::Copy($raw, 0, $out, 0, $len)
        $raw = $null
        return , $out
    }
    return , $raw
}

# Measure-PeakRssDuring 在后台以 50ms 采样监听端口的进程 RSS，返回窗口内的**峰值**。
#
# 为什么不能用「上传前后各取一次」：Go 的 scavenger 会把空闲堆还给操作系统，
# 实测同一次上传的「增量」可以是 **-111.7 MB**（139.5 → 27.8）—— 那个数字既不能
# 证明流式、也不能证伪，只说明两个采样点不可比。判据要的是「**流式期间**有没有涨」，
# 所以必须在上传**进行中**采样，并且取峰值而不是终值。
# 停止信号用文件而不是超时：采样窗口必须与上传同长，而不是拍一个固定秒数。
function Start-RssSampler {
    param([int]$Port, [string]$StopFile, [string]$ReadyFile)
    return Start-Job -ArgumentList $Port, $StopFile, $ReadyFile -ScriptBlock {
        param($Port, $StopFile, $ReadyFile)
        $peak = -1.0
        $ready = $false
        while (-not (Test-Path $StopFile)) {
            $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($c) {
                $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
                if ($p) {
                    $mb = [double]($p.WorkingSet64 / 1MB)
                    if ($mb -gt $peak) { $peak = $mb }
                }
            }
            # 握手：采到第一个点才算「就绪」，主进程等到这个文件再开始上传。
            # 不写这行就是**竞态**：job 是新起的 powershell 进程，冷启动
            # 0.5~1.5s，而 8MB 上传只要约 1s —— job 常在上传结束后才第一次
            # 检查停止文件，于是「一次都没采样」直接返回 -1（症状：peak=-1，
            # 且时好时坏；同一份脚本上一次 40 PASS、下一次就 SKIP）。
            if (-not $ready -and $peak -gt 0) {
                Set-Content -Path $ReadyFile -Value "ready" -Encoding ASCII
                $ready = $true
            }
            Start-Sleep -Milliseconds 50
        }
        return $peak
    }
}

function Invoke-Upload {
    param([string]$Url, [string]$Token, [byte[]]$Body, [string]$Boundary, [int]$TimeoutSec = 180)
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.Method = "POST"
    $req.ContentType = "multipart/form-data; boundary=$Boundary"
    $req.ContentLength = $Body.Length
    $req.Timeout = $TimeoutSec * 1000
    $req.ReadWriteTimeout = $TimeoutSec * 1000
    $req.Headers.Add("X-Request-Id", "req_stage5_upload")
    if ($Token) { $req.Headers.Add("Authorization", "Bearer $Token") }
    try {
        $s = $req.GetRequestStream()
        $s.Write($Body, 0, $Body.Length)
        $s.Close()
    } catch {
        return [pscustomobject]@{ Status = 0; Text = ("发送失败：" + $_.Exception.Message) }
    }
    try {
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        return [pscustomobject]@{ Status = [int]$resp.StatusCode; Text = $sr.ReadToEnd() }
    } catch {
        $webErr = $_.Exception
        while ($null -ne $webErr -and ($webErr -isnot [System.Net.WebException])) { $webErr = $webErr.InnerException }
        $r = $null
        if ($webErr) { $r = $webErr.Response }
        if ($r) {
            $sr = New-Object System.IO.StreamReader($r.GetResponseStream(), [System.Text.Encoding]::UTF8)
            return [pscustomobject]@{ Status = [int]$r.StatusCode; Text = $sr.ReadToEnd() }
        }
        return [pscustomobject]@{ Status = 0; Text = $_.Exception.Message }
    }
}

# 监听端口的进程 RSS（AC-ORCH-05 的「网关进程 RSS 增长 < 50MB」）。
function Get-ListenerRss {
    param([int]$Port)
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return -1 }
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if (-not $p) { return -1 }
    return [double]($p.WorkingSet64 / 1MB)
}

# ---- 审计查询（直连 MySQL）----
#
# 从 `.env` 解析 MYSQL_DSN 而不是把口令写死在脚本里：
# 脚本会进 git（`.env` 不会），凭据一旦写进来就是永久泄漏。
function Get-MysqlCredential {
    $envFile = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in (Get-Content -Encoding UTF8 $envFile)) {
        if ($line -match '^\s*MYSQL_DSN\s*=\s*([^:]+):([^@]+)@tcp\(([^:]+):(\d+)\)/([^?]+)') {
            return [pscustomobject]@{
                User = $Matches[1]; Pass = $Matches[2]
                Host = $Matches[3]; Port = $Matches[4]; DB = $Matches[5]
            }
        }
    }
    return $null
}

function Get-AuditRows {
    param([string]$Email, [string]$Action)
    $mysql = Get-Command mysql -ErrorAction SilentlyContinue
    if (-not $mysql) { return -1 }
    $c = Get-MysqlCredential
    if (-not $c) { return -1 }
    # 邮箱在 detail 里是脱敏后的（cryptox.MaskEmail），因此按 action 计数、
    # 按「最近 5 分钟」限定本次脚本产生的行，而不是拿邮箱做匹配。
    #
    # ⚠️ 必须用 `UTC_TIMESTAMP()` 而不是 `NOW()`：docs/05-§2 规定时间列一律
    # 存 UTC（网关的 DSN 带 `loc=UTC`，go-sql-driver 写库前会把时间转成 UTC），
    # 而 `NOW()` 返回的是 MySQL 会话时区的本地时间。本机会话时区是 +08:00，
    # 于是 `created_at >= NOW() - INTERVAL 5 MINUTE` 拿 UTC 的 04:xx 去比
    # 本地时间的 12:xx ⇒ **永远 0 行**，而拦截本身完全正常 ——
    # 表现为「审计没落库」的假告警，正是文档里点名的「差 8 小时的排序」。
    $sql = "SELECT COUNT(*) FROM audit_log WHERE action='$Action' AND created_at >= (UTC_TIMESTAMP() - INTERVAL 5 MINUTE);"
    $out = & $mysql.Source --host=$($c.Host) --port=$($c.Port) --user=$($c.User) `
        --password=$($c.Pass) --skip-column-names --batch $($c.DB) -e $sql 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return -1 }
    return [int]($out | Select-Object -Last 1)
}

# ----------------------------------------------------------------------
$ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$email = "m5_$ts@example.com"
$pass = 'M5-Passw0rd!'

Write-Host ""
Write-Host "== 0. 准备：编译一个可复用的网关二进制 ==" -ForegroundColor Cyan
# 用编译产物而不是 `go run`：本脚本要拉两个临时实例，
# `go run` 每次都要重新编译（每个实例多花约 10s），而且**拿不到稳定的进程句柄**
# （`go run` 的进程树是 go → server，`Start-Process -PassThru` 拿到的是 go 的 pid）。
$exe = Join-Path $env:TEMP "gw-stage5.exe"
$buildLog = Join-Path $env:TEMP "gw-stage5-build.log"
& go build -o $exe ./cmd/server 2>&1 | Out-File -Encoding UTF8 $buildLog
Assert-True "go build 成功（$exe）" (Test-Path $exe) "见 $buildLog"

Write-Host ""
Write-Host "== 1. 注册 / 登录（主网关）==" -ForegroundColor Cyan
$reg = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{ email = $email; password = $pass }
Assert-True "注册 201" ($reg.Status -eq 201) "status=$($reg.Status) body=$($reg.Text)"
$login = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $email; password = $pass }
Assert-True "登录 200" ($login.Status -eq 200) "status=$($login.Status)"
$token = $login.Json.access_token
Assert-True "拿到 access_token" ([bool]$token) "len=$(($token | Measure-Object -Character).Characters)"

try {
    # ------------------------------------------------------------------
    if (-not $SkipUpload) {
        Write-Host ""
        Write-Host "== 2. S4 上传流式转发（AC-ORCH-05）==" -ForegroundColor Cyan

        $kb = Invoke-Api -Method POST -Path "/api/v1/knowledge-bases" -Token $token -Body @{ name = "m5-$ts" }
        Assert-True "建知识库 201" ($kb.Status -eq 201) "status=$($kb.Status) body=$($kb.Text)"
        $kbId = $kb.Json.id
        Assert-Info "kb_id" "$kbId"

        # 造一个 $UploadMB MB 的 markdown（用可读文本而不是全 0：
        # AI 侧要按行切分，全 0 会被当成二进制/空文档）。
        $line = "# 上传流式转发测试`n`n这是第 {0} 段占位文本，用于把文件撑到指定大小。`n"
        $content = New-TextBody -TargetBytes ([int64]$UploadMB * 1MB) -Line $line
        Assert-Info "上传体积" ("{0:N2} MB（目标 {1} MB）" -f ($content.Length / 1MB), $UploadMB)
        # 上限自检：正文必须**小于**网关默认的 UPLOAD_MAX_MB，否则这条断言会
        # 静默变成一条 413 测试（下面马上又有一条用 UPLOAD_MAX_MB=1 验 413）。
        Assert-True "上传体积低于默认上限（50MB）" (($content.Length / 1MB) -lt 50) `
            "size=$([Math]::Round($content.Length / 1MB, 2))MB"
        Assert-True "上传体积贴合目标（误差 < 1MB）" `
            ([Math]::Abs($content.Length / 1MB - $UploadMB) -lt 1) `
            "size=$([Math]::Round($content.Length / 1MB, 2))MB target=$UploadMB"
        # 结构守卫：必须是**真** byte[]，不能是 Object[]。
        #
        # 这条断言直接钓的是导致本脚本 `OutOfMemoryException` 的那个写法：
        # PowerShell 的 `$bytes[0..$n]` 切片在 `byte[]` 上返回 `Object[]`，
        # 每个字节都被**装箱**（实测 8MB 切片 → RSS **+618 MB**）。
        # 症状是「数据完全正确、断言全过」—— 只有内存炸了，所以必须显式断言类型：
        # 长度、内容、字节数都看不出差别。
        Assert-True '正文是 byte[]（不是 Object[]，无装箱）' ($content -is [byte[]]) `
            "type=$($content.GetType().FullName) len=$($content.Length)"
        Assert-True '正文元素类型是 Byte（未装箱）' ($content.Length -eq 0 -or $content[0] -is [byte]) `
            "elem=$($content[0].GetType().FullName)"

        $rssBefore = Get-ListenerRss -Port $MainPort
        $boundary = "----stage5" + [Guid]::NewGuid().ToString("N")
        # 本进程的内存自检：造完 body 后自己涨了多少。
        # 不是产品判据（产品的 RSS 在网关那边），而是**脚本自身**的守卫：
        # 它把「脚本把自己撑爆」这类问题变成一条具名失败，而不是偶发的 OOM 崩溃
        # （OOM 崩在没有输出的地方，看不到是哪一步）。
        $scriptRssBefore = [int]((Get-Process -Id $PID).WorkingSet64 / 1MB)
        $body = New-MultipartBody -FileName "m5.txt" -Content $content -Boundary $boundary
        $scriptRssAfter = [int]((Get-Process -Id $PID).WorkingSet64 / 1MB)
        Assert-True '正文是 byte[]（不是 Object[]）' ($body -is [byte[]]) `
            "type=$($body.GetType().FullName) len=$($body.Length)"
        $scriptGrowth = $scriptRssAfter - $scriptRssBefore
        Assert-Info '本进程 RSS（造 body 前后）' `
            ("{0} MB → {1} MB（增量 {2} MB）" -f $scriptRssBefore, $scriptRssAfter, $scriptGrowth)
        # 门槛：字节数的 4 倍 + 64MB 余量。正常实现（一次分配到位）远低于此；
        # 装箱实现是字节数的 ~30 倍，会立刻越过。
        $bodyBudget = [int](4 * $UploadMB) + 64
        Assert-True ("造 body 的内存增量 < $bodyBudget MB（无装箱/无多次拷贝）") `
            ($scriptGrowth -lt $bodyBudget) `
            "growth=$scriptGrowth budget=$bodyBudget"
        $stopFile = Join-Path $env:TEMP ("stage5-rss-" + $ts + ".stop")
        $readyFile = Join-Path $env:TEMP ("stage5-rss-" + $ts + ".ready")
        Remove-Item $stopFile, $readyFile -ErrorAction SilentlyContinue
        $sampler = Start-RssSampler -Port $MainPort -StopFile $stopFile -ReadyFile $readyFile
        # 等采样作业宣布「我至少采到一个点」再上传，见 Start-RssSampler 里的握手注释。
        $readyDeadline = (Get-Date).AddSeconds(10)
        while (-not (Test-Path $readyFile) -and (Get-Date) -lt $readyDeadline) {
            Start-Sleep -Milliseconds 50
        }
        if (-not (Test-Path $readyFile)) {
            Write-Host "  （采样作业 10s 未就绪，RSS 两条判据会降级为 SKIP）" -ForegroundColor Yellow
        }
        try {
            $up = Invoke-Upload -Url "$BaseUrl/api/v1/knowledge-bases/$kbId/documents" -Token $token -Body $body -Boundary $boundary
        } finally {
            # 无论上传成功与否都要停掉采样作业，否则它会在后台一直转
            Set-Content -Path $stopFile -Value "stop" -Encoding ASCII
            Remove-Item $readyFile -ErrorAction SilentlyContinue
        }
        $rssPeak = [double](Receive-Job -Job $sampler -Wait -ErrorAction SilentlyContinue | Select-Object -Last 1)
        Remove-Job -Job $sampler -Force -ErrorAction SilentlyContinue
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        $body = $null   # 同上：49MB 的 body 留着会把后面的 multipart 拼装挤爆

        Assert-True "上传返回 202（异步任务）" ($up.Status -eq 202) "status=$($up.Status) body=$($up.Text.Substring(0, [Math]::Min(300, $up.Text.Length)))"
        $upJson = $null
        try { $upJson = $up.Text | ConvertFrom-Json } catch { $upJson = $null }
        Assert-True "回包含 task_id" ([bool]($upJson -and $upJson.task_id)) "body=$($up.Text.Substring(0, [Math]::Min(200, $up.Text.Length)))"

        # UP-01：截断必须对调用方**可见**。
        #
        # 8MB 正文切出 16969 片，被 `MAX_DOC_CHUNKS=10000` 截断。没有这两个字段时，
        # 调用方只能看到 `chunk_count=10000`，**无法区分**「文件本来就这么大」与
        # 「被静默截断了」—— 后者是丢数据。
        #
        # ⚠️ 口径（实测确认，别写反）：`document.chunks_total` 是**截断前**产出数
        # （16969），而 `task.chunks_total` 是**本轮要处理的片数**（截断后 10000）。
        # 上游是刻意不混用的两个量；断言标的必须是前者，写成后者会「通过但没验到东西」。
        #
        # 只等 `chunks_total` 出现（CHUNKING 之后即可见），**不等终态**：
        # 1 万片在 CPU 上跑 BGE 要几分钟（实测 49MB 那版把 AI 侧 /health 拖到不应答）。
        # 这里要证的是「字段通到了 API」，不是「向量化跑完」。
        #
        # 顺带这是**网关透传**的回归位：网关若用强类型 DTO 解上游 JSON 再转发，
        # 新字段会被静默丢掉（docs/10 §8-6），届时这里会红。
        $docDeadline = (Get-Date).AddSeconds(60)
        $docTotal = -1
        $docTrunc = $false
        $docCount = -1
        $docStatus = ""
        while ((Get-Date) -lt $docDeadline) {
            $dl = Invoke-Api -Method GET -Path "/api/v1/knowledge-bases/$kbId/documents" -Token $token
            if ($dl.Status -eq 200 -and $dl.Json) {
                $items = @($dl.Json.items)
                if ($items.Count -gt 0) {
                    $d0 = $items[0]
                    $docStatus = [string]$d0.status
                    if ($null -ne $d0.chunks_total) { $docTotal = [int]$d0.chunks_total }
                    $docTrunc = [bool]$d0.truncated
                    $docCount = [int]$d0.chunk_count
                    if ($docTotal -gt 0) { break }
                }
            }
            Start-Sleep -Milliseconds 500
        }
        Assert-True "文档带 chunks_total（截断前总数）" ($docTotal -gt 0) `
            "status=$docStatus chunks_total=$docTotal"
        Assert-True "chunks_total > 10000（MAX_DOC_CHUNKS，8MB 必被截断）" ($docTotal -gt 10000) `
            "chunks_total=$docTotal"
        Assert-True "truncated 标记为真" ($docTrunc) `
            "truncated=$docTrunc chunk_count=$docCount chunks_total=$docTotal"

        # 判据：**流式期间**的峰值增量远小于文件体积 ⇒ 没有整读进内存。
        # 取峰值而不是终值，见 Start-RssSampler 的注释（终值会被 GC 归还内存搞成负数）。
        if ($rssBefore -gt 0 -and $rssPeak -gt 0) {
            $growth = $rssPeak - $rssBefore
            Assert-Info "网关 RSS" ("{0:N1} MB → 峰值 {1:N1} MB（增量 {2:N1} MB）" -f $rssBefore, $rssPeak, $growth)
            # 留 50MB 绝对值余量与 AC-ORCH-05 一致，同时加一条「不超过文件本身」的
            # 相对判据 —— 文件越大，非流式实现泄漏出来的增量越显著。
            Assert-True "RSS 峰值增量 < 50MB" ($growth -lt 50) "growth=$growth before=$rssBefore peak=$rssPeak"
            Assert-True "RSS 峰值增量 < 文件体积" ($growth -lt ($content.Length / 1MB)) `
                "growth=$growth file=$([Math]::Round($content.Length / 1MB, 2))MB"
        } else {
            Assert-Skip "RSS 对比" "采样未成立（before=$rssBefore peak=$rssPeak 端口=$MainPort；before<0 才是拿不到进程）"
        }

        # 超限：用小上限的实例验 413（不该为了这条断言真传 51MB）。
        #
        # ⚠️ 这里**换一份小 body**（2MB，远小于默认 50MB、远大于这个小实例的 1MB），
        # 而不是复用上面那份 8MB：两份大 body 同时活着是不必要的峰值（`$content`
        # 要一直留到上面的相对判据算完）。而且 413 是**前置判断**，body 内容对它
        # 没有任何影响。
        $small = Start-AuxGateway -Exe $exe -Port ($DeadPort + 10) -MetricsPort ($DeadMetricsPort + 10) `
            -LogPath (Join-Path $env:TEMP "gw-stage5-small.log") -ExtraEnv @{ UPLOAD_MAX_MB = "1" }
        if ($small) {
            $login2 = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Base ("http://127.0.0.1:" + ($DeadPort + 10)) -Body @{ email = $email; password = $pass }
            $token2 = $login2.Json.access_token
            $b2 = "----stage5small" + [Guid]::NewGuid().ToString("N")
            $smallContent = New-TextBody -TargetBytes (2MB) -Line $line
            $body2 = New-MultipartBody -FileName "big.txt" -Content $smallContent -Boundary $b2
            $smallContent = $null
            $up2 = Invoke-Upload -Url ("http://127.0.0.1:" + ($DeadPort + 10) + "/api/v1/knowledge-bases/$kbId/documents") -Token $token2 -Body $body2 -Boundary $b2
            $body2 = $null
            $o2 = $null
            try { $o2 = $up2.Text | ConvertFrom-Json } catch { $o2 = $null }
            Assert-True "超过 UPLOAD_MAX_MB → 413 PAYLOAD_TOO_LARGE" `
                ($up2.Status -eq 413 -and $o2 -and $o2.error.code -eq 'PAYLOAD_TOO_LARGE') `
                "status=$($up2.Status) body=$($up2.Text.Substring(0, [Math]::Min(200, $up2.Text.Length)))"
        } else {
            Assert-Skip "413 超限断言" "小上限实例没起来"
        }
    } else {
        Write-Host "  SKIP  S4 上传（-SkipUpload）" -ForegroundColor DarkYellow
    }

    # ------------------------------------------------------------------
    if (-not $SkipQuota) {
        Write-Host ""
        Write-Host "== 3. S5 配额耗尽（AC-AUTH-06 / E2E-4）==" -ForegroundColor Cyan

        # free 档只给 1 次对话：第 2 次必须被拦，且**不能**打给 AI。
        $planMap = '{"free":{"chat_requests":1,"llm_tokens":1000000,"kb_count":10,"documents_count":20,"storage_bytes":1073741824,"concurrency":5}}'
        $qLog = Join-Path $env:TEMP "gw-stage5-quota.log"
        $qp = Start-AuxGateway -Exe $exe -Port $QuotaPort -MetricsPort $QuotaMetricsPort -LogPath $qLog `
            -ExtraEnv @{ QUOTA_PLAN_MAP = $planMap }
        if (-not $qp) {
            Assert-True "配额实例启动" $false "见 $qLog"
        } else {
            $qBase = "http://127.0.0.1:$QuotaPort"
            $qEmail = "m5q_$ts@example.com"
            $null = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Base $qBase -Body @{ email = $qEmail; password = $pass }
            $qLogin = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Base $qBase -Body @{ email = $qEmail; password = $pass }
            $qToken = $qLogin.Json.access_token
            $qConv = Invoke-Api -Method POST -Path "/api/v1/conversations" -Base $qBase -Token $qToken -Body @{ title = "quota" }
            $qConvId = $qConv.Json.id
            $qPath = "/api/v1/conversations/$qConvId/messages"

            $aiOK1 = Get-MetricValue -MetricsPort $QuotaMetricsPort -Pattern '^gw_ai_requests_total\{operation="chat",result="ok"\}'
            $m1 = Invoke-Api -Method POST -Path $qPath -Base $qBase -Token $qToken -Body @{ content = '只回两个字：好的'; use_rag = $false }
            $m2 = Invoke-Api -Method POST -Path $qPath -Base $qBase -Token $qToken -Body @{ content = '再来一次'; use_rag = $false }
            $aiOK2 = Get-MetricValue -MetricsPort $QuotaMetricsPort -Pattern '^gw_ai_requests_total\{operation="chat",result="ok"\}'

            Assert-True "第 1 次对话成功（限额内）" ($m1.Status -eq 200) "status=$($m1.Status) body=$($m1.Text.Substring(0, [Math]::Min(200, $m1.Text.Length)))"
            Assert-True "第 2 次被拦：429" ($m2.Status -eq 429) "status=$($m2.Status) body=$($m2.Text.Substring(0, [Math]::Min(200, $m2.Text.Length)))"
            $code2 = $null
            if ($m2.Json -and $m2.Json.error) { $code2 = $m2.Json.error.code }
            Assert-True "错误码 QUOTA_EXCEEDED" ($code2 -eq 'QUOTA_EXCEEDED') "code=$code2"
            Assert-True "错误信封带 trace_id" ([bool]($m2.Json.error.trace_id)) "trace=$($m2.Json.error.trace_id)"
            Assert-True "响应头 X-Trace-Id 与信封一致" ($m2.Trace -eq $m2.Json.error.trace_id) "header=$($m2.Trace) envelope=$($m2.Json.error.trace_id)"
            # 核心断言：**被拦的那次没有打给 AI**。只看 429 是不够的 ——
            # 「先打给 AI 再判配额」也会返回 429，但白花了一次模型调用。
            Assert-True "AI 侧调用计数只增加 1（被拦的那次没打给 AI）" (($aiOK2 - $aiOK1) -eq 1) "before=$aiOK1 after=$aiOK2"
            $qMetric = Get-MetricValue -MetricsPort $QuotaMetricsPort -Pattern '^gw_quota_exceeded_total\{metric="chat_requests"\}'
            Assert-True "gw_quota_exceeded_total{metric=chat_requests} == 1" ($qMetric -eq 1) "value=$qMetric"

            # 回滚：被拦的那次不该把计数器留在「已用」上。
            $q = Invoke-Api -Method GET -Path "/api/v1/me/quota" -Base $qBase -Token $qToken
            $usedItems = @($q.Json.metrics | Where-Object { $_.metric -eq 'chat_requests' })
            if ($usedItems.Count -gt 0) {
                Assert-Info "chat_requests 用量" "used=$($usedItems[0].used) limit=$($usedItems[0].limit)"
                Assert-True "用量被截在 limit 上（未被第 2 次多加）" ([int64]$usedItems[0].used -le [int64]$usedItems[0].limit) `
                    "used=$($usedItems[0].used) limit=$($usedItems[0].limit)"
            } else {
                Assert-Skip "用量回读" "/me/quota 未列出 chat_requests"
            }

            # 审计：配额超限 MUST 落审计（docs/05-§2.8）。
            $audit = Get-AuditRows -Email $qEmail -Action "quota_exceeded"
            Assert-True "audit_log 有 quota_exceeded 行" ($audit -ge 1) "rows=$audit"
        }
    } else {
        Write-Host "  SKIP  S5 配额（-SkipQuota）" -ForegroundColor DarkYellow
    }

    # ------------------------------------------------------------------
    if (-not $SkipAiDown) {
        Write-Host ""
        Write-Host "== 4. S7 降级（AC-ORCH-09）+ 熔断（AC-ORCH-08）==" -ForegroundColor Cyan

        # 上游指向**必然连不上**的端口：这比「真的停掉 AI」更好 ——
        # ① 不影响同一台机器上的其它脚本；② 「AI 侧一次调用都没收到」可由端口必然空闲直接推出。
        $dLog = Join-Path $env:TEMP "gw-stage5-dead.log"
        $dp = Start-AuxGateway -Exe $exe -Port $DeadPort -MetricsPort $DeadMetricsPort -LogPath $dLog `
            -ExtraEnv @{ AI_PLATFORM_GRPC_TARGET = "127.0.0.1:59999"; AI_PLATFORM_BASE_URL = "http://127.0.0.1:59999" }
        if (-not $dp) {
            Assert-True "降级实例启动" $false "见 $dLog"
        } else {
            $dBase = "http://127.0.0.1:$DeadPort"
            $dEmail = "m5d_$ts@example.com"
            $null = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Base $dBase -Body @{ email = $dEmail; password = $pass }
            $dLogin = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Base $dBase -Body @{ email = $dEmail; password = $pass }
            $dToken = $dLogin.Json.access_token
            $dConv = Invoke-Api -Method POST -Path "/api/v1/conversations" -Base $dBase -Token $dToken -Body @{ title = "degraded" }
            $dConvId = $dConv.Json.id

            Assert-True "上游不可达时登录仍 200" ($dLogin.Status -eq 200) "status=$($dLogin.Status)"
            Assert-True "上游不可达时建会话仍 201" ($dConv.Status -eq 201) "status=$($dConv.Status)"

            $convList = Invoke-Api -Method GET -Path "/api/v1/conversations" -Base $dBase -Token $dToken
            Assert-True "AC-ORCH-09：GET /conversations 仍 200" ($convList.Status -eq 200) "status=$($convList.Status)"
            $quotaGet = Invoke-Api -Method GET -Path "/api/v1/me/quota" -Base $dBase -Token $dToken
            Assert-True "AC-ORCH-09：GET /me/quota 仍 200" ($quotaGet.Status -eq 200) "status=$($quotaGet.Status)"

            # 连续打到熔断打开。`cb_threshold` 默认 10。
            #
            # 探针用 20s 超时（而不是默认 120s）：修复前这里有一个**自死锁** ——
            # `AICircuitBreaker.logf` 在持有 `b.mu` 时调 `Snapshot()`，而 `Snapshot()`
            # 要加同一把锁，于是「连续 10 次失败 → 熔断打开」那一步把自己锁死，
            # 之后每个请求都卡满 120s 才超时，**且一行日志都没有**。
            # 20s 让这类挂起在几十秒内就暴露成 status=0，而不是三分钟的静默等待。
            $probeTimeoutSec = 20
            $statuses = New-Object System.Collections.ArrayList
            $codes = New-Object System.Collections.ArrayList
            $ms = New-Object System.Collections.ArrayList
            for ($i = 1; $i -le ($CBFailures + 2); $i++) {
                $r = Invoke-Api -Method POST -Path "/api/v1/conversations/$dConvId/messages" -Base $dBase -Token $dToken `
                    -Body @{ content = "probe $i"; use_rag = $false } -TimeoutSec $probeTimeoutSec
                [void]$statuses.Add($r.Status)
                $c = ""
                if ($r.Json -and $r.Json.error) { $c = $r.Json.error.code }
                [void]$codes.Add($c)
                [void]$ms.Add([int]$r.Ms)
            }
            Assert-Info "逐次结果" (($statuses -join ","))
            Assert-Info "错误码序列" (($codes -join ","))
            Assert-Info "耗时 ms" (($ms -join ","))

            $last = $statuses[$statuses.Count - 1]
            Assert-True "上游不可达 → 503" ($last -eq 503) "last=$last"
            Assert-True "错误码为 AI_UNAVAILABLE 或 DEPENDENCY_UNAVAILABLE" `
                ($codes[$codes.Count - 1] -in @('AI_UNAVAILABLE', 'DEPENDENCY_UNAVAILABLE')) "last=$($codes[$codes.Count - 1])"

            # 熔断打开之后**不再发起调用**：这是 AC-ORCH-08 的实质。
            # 断言「拒绝路径很快」，因为「慢」正是熔断要消灭的东西 ——
            # AI 全挂时如果每个请求还要撞一次连接超时，p99 就变成上游超时的量级。
            # 门槛取 2s：真实实现是纯内存判断 + 一次日志写（实测个位数毫秒）。
            $rejectMs = $ms[$ms.Count - 1]
            Assert-True "熔断打开后拒绝路径 < 2000ms（不阻塞）" ($rejectMs -lt 2000) "ms=$rejectMs"
            $hung = @($ms | Where-Object { $_ -ge ($probeTimeoutSec * 1000) }).Count
            Assert-True "没有请求打到脚本超时（无阻塞/死锁）" ($hung -eq 0) "hung=$hung timeouts=$($ms -join ',')"

            $cbState = Get-MetricValue -MetricsPort $DeadMetricsPort -Pattern '^gw_circuit_breaker_state\{target="ai-platform"\}'
            Assert-True "gw_circuit_breaker_state == 2（打开）" ($cbState -eq 2) "state=$cbState"
            $rejected = Get-MetricValue -MetricsPort $DeadMetricsPort -Pattern '^gw_ai_requests_total\{operation="chat",result="rejected"\}'
            Assert-True "gw_ai_requests_total{result=rejected} >= 1（熔断拦截有计数）" ($rejected -ge 1) "value=$rejected"
            $errSeries = Get-SeriesCount -MetricsPort $DeadMetricsPort -Prefix "gw_ai_errors_total{"
            Assert-True "gw_ai_errors_total 有样本（错误码分布可查）" ($errSeries -ge 1) "series=$errSeries"
            # 熔断状态变化必须留日志（`circuit.open`）—— 修复前这一步死锁，
            # 于是指标显示 state=2 但日志里一个字都没有。
            $openLines = @(Get-Content $dLog -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-String -Pattern 'circuit\.open' -SimpleMatch:$false).Count
            Assert-True "日志里有 circuit.open（状态变化可观测，未死锁）" ($openLines -ge 1) "lines=$openLines"
        }
    } else {
        Write-Host "  SKIP  S7 降级 / 熔断（-SkipAiDown）" -ForegroundColor DarkYellow
    }

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 5. 限流按真实 IP（AC-NFR-07，在主网关 18080 上做）==" -ForegroundColor Cyan
    Write-Host "      注意：限流桶是**全机器共享**的 Redis 键（按真实 IP），"
    Write-Host "      因此本段放在最后跑，且不假设「第 11 次」这种绝对次数。" -ForegroundColor DarkGray

    $bogus = "m5rl_$ts@example.com"
    $limitedAt = -1
    $limitCode = ""
    for ($i = 1; $i -le 25; $i++) {
        $r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $bogus; password = "Wrong-Passw0rd!" }
        if ($r.Status -eq 429) {
            $limitedAt = $i
            if ($r.Json -and $r.Json.error) { $limitCode = $r.Json.error.code }
            break
        }
    }
    Assert-True "连续错误登录后出现 429" ($limitedAt -gt 0) "第 $limitedAt 次仍未限流"
    Assert-True "限流错误码 RATE_LIMITED" ($limitCode -eq 'RATE_LIMITED') "code=$limitCode"

    # 伪造 X-Forwarded-For 必须**无效**：TRUSTED_PROXY_COUNT 默认 0。
    $stillLimited = $true
    for ($i = 0; $i -lt 3; $i++) {
        $r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $bogus; password = "Wrong-Passw0rd!" } `
            -ExtraHeaderName "X-Forwarded-For" -ExtraHeaderValue "1.2.3.4"
        if ($r.Status -ne 429) { $stillLimited = $false }
    }
    Assert-True "伪造 X-Forwarded-For 不能绕过限流（仍 429）" $stillLimited "存在未被限流的响应"
    $authFailPort = 9107
    if (-not $SkipQuota) { $authFailPort = $QuotaMetricsPort }
    $authFail = Get-MetricValue -MetricsPort $authFailPort -Pattern '^gw_auth_failures_total\{reason="'
    Assert-Info "鉴权失败指标可查" "value=$authFail"
}
finally {
    Write-Host ""
    Write-Host "== 清理：停掉本脚本拉起的临时网关 ==" -ForegroundColor Cyan
    foreach ($p in $script:Aux) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Write-Host ("  stopped pid=" + $p.Id) -ForegroundColor DarkGray
        }
    }
}

Write-Host ""
if ($script:Failed -eq 0) {
    Write-Host ("=== M5 验收：全部通过（" + $script:Total + " 项）===") -ForegroundColor Green
} else {
    Write-Host ("=== M5 验收：失败 " + $script:Failed + " / " + $script:Total + " ===") -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host ("  - " + $f) -ForegroundColor Red }
}
exit 0
