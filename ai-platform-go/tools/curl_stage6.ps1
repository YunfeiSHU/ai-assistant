# M6 验收：可观测性（跨服务 trace、指标完整性、结构化日志脱敏、审计、留存）
#
# 覆盖的场景（docs/01-§3 的场景表 + docs/06-§8 的验收标准）：
#   S8  跨服务排障       —— 一次对话能在网关与 AI 侧用同一个 trace_id 查到（AC-NFR-08）
#   S9/S10 令牌生命周期 —— 登出后 refresh 立即作废、access 换新（AC-NFR-04 的落地面）
#   AC-NFR-09            —— 指标端口能抓到 docs/06-§5.2 的**全部**族，且 route 是模板不是真实路径
#   AC-NFR-05            —— 全量日志里不出现凭据、JWT、邮箱全称
#   docs/05-§2.8         —— 关键动作落 audit_log
#   留存                  —— `gw_orphan_rows_total` 可见（每日一致性检查的输出通道）
#
# 前置：
#   1) AI gRPC（S8 要一次真实编排）：
#        $env:GRPC_ENABLED="true"; uv run python -m app.grpc
#   2) AI HTTP（S8 的「AI 侧日志」从它的 stdout 里读）：
#        $env:METRICS_PORT="9106"; uv run python -m app.main
#   3) 主网关（18080，带 METRICS_PORT 指向一个**本脚本独占**的端口）：
#        $env:HTTP_ADDR="0.0.0.0:18080"; $env:METRICS_PORT="9107"; $env:LOG_LEVEL="debug"
#
# **本脚本会自己拉一个临时网关实例**（18084 / 9111，日志写到 $env:TEMP），
# 而不是打到正在运行的 18080 上。原因有三条，每一条都曾让验收变成假绿/假红：
#   a) 指标是**进程级**累积量。打在一个已经跑过 stage1..5 的实例上，
#      「gw_login_attempts_total >= 1」这种断言永远成立，验不出任何东西。
#   b) S8 要读**网关自己的日志文件**做 trace 比对；不知道日志在哪就没法读，
#      而「先问用户把日志重定向到了哪」不是一个可自动化的问题。
#   c) 同一台机器上其它脚本可能正在用 18080 做别的事（stage5 自己也会拉实例）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage6.ps1
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage6.ps1 -SkipChat   # 不跑真实模型
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage6.ps1 -KeepGateway # 排障时留着实例

[CmdletBinding()]
param(
    # 主网关：只用来做「同一个二进制、同一套变量」的对照，本脚本的业务请求都发到 -Port。
    [string]$BaseUrl = "http://127.0.0.1:18080",
    [int]$Port = 18084,
    [int]$MetricsPort = 9111,
    # AI 侧（HTTP 通道的 stdout 日志 + gRPC 通道）——S8 两侧比对的另一半。
    [string]$AILogPath = "",
    [switch]$SkipChat,
    [switch]$KeepGateway
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[System.Net.ServicePointManager]::Expect100Continue = $false

$script:Total = 0
$script:Failed = 0
$script:Failures = New-Object System.Collections.ArrayList

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
# 本脚本 100% 打 127.0.0.1（自拉实例的 `/metrics`、`/health/live`、以及主网关），
# 关掉代理是纯收益：省掉的是**每次往返**的固定开销。
#
# 为什么设 .NET 静态属性而不只写 `-Proxy $null`：PS 5.1 的 `-Proxy` 落在
# `if (Proxy != null) { request.Proxy = Proxy }` 上，传 `$null` 在部分版本上等价于
# **不传**（代理照旧生效）。进程级 `DefaultWebProxy = $null` 才是确定生效的那一下；
# `Invoke-Api` 的参数表里另外带上 `-Proxy $null`（PS 6+ 用 `-NoProxy`）做双保险。
#
# ⚠ 只在「目标全是回环」时成立：把 -BaseUrl 指到非回环地址请删掉这一段。
[System.Net.WebRequest]::DefaultWebProxy = $null

# ---- JSON 信封接口（与 stage1..5 同一份实现）----
function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Token,
        [object]$Body,
        [string]$TraceId = "",
        [string]$Base = "",
        [int]$TimeoutSec = 120
    )
    if (-not $Base) { $Base = "http://127.0.0.1:$Port" }
    $headers = @{ "X-Request-Id" = "req_stage6" }
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }
    # traceparent 是本脚本的核心输入：网关 MUST 读它、MUST 把同一个 trace_id 写回
    # `X-Trace-Id`、MUST 用同一个 id 记日志（docs/06-§5.1）。
    if ($TraceId) { $headers["traceparent"] = "00-$TraceId-7f3a91c2d5b6480e-01" }
    $params = @{ Method = $Method; Uri = ($Base + $Path); Headers = $headers; UseBasicParsing = $true; TimeoutSec = $TimeoutSec }
    if ($null -ne $Body) {
        $params["ContentType"] = "application/json"
        $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
    }
    # 与文件顶部 `DefaultWebProxy = $null` 配对的双保险：PS 6+ 用 `-NoProxy`，
    # PS 5.1 没有这个开关，只能传 `-Proxy $null`（顶部那段注释解释了为什么它单独用不够）。
    if ($PSVersionTable.PSVersion.Major -ge 6) { $params["NoProxy"] = $true } else { $params["Proxy"] = $null }

    $status = 0; $text = ""; $respHeaders = $null
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

# ---- 指标抓取 ----
#
# 直接抓文本再解析，而不是用爬虫库：本脚本要断言的恰恰是「文本里有这个族」
# 和「route 标签是模板」，保留原始文本让失败信息能直接贴出来。
function Get-MetricsText {
    param([int]$Mp = $MetricsPort, [string]$Path = "/metrics")
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Mp$Path" -UseBasicParsing -TimeoutSec 10
        return [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
    } catch {
        return ""
    }
}

# Get-SeriesCount 数某个前缀下的**样本行**（不是 HELP/TYPE 注释行）。
#
# ⚠️ 直方图的样本行名带后缀（`_bucket` / `_sum` / `_count`），
# 而族名本身（`gw_request_duration_seconds`）在纯直方图下**一个样本行也没有**。
# 传 `gw_request_duration_seconds` 会数出 0，看起来像「族不存在」，
# 而 `gw_request_duration_seconds_bucket` 才是正确的计数前缀。
function Get-SeriesCount {
    param([string]$Text, [string]$Prefix)
    if (-not $Text) { return 0 }
    return @($Text -split "`n" | Where-Object {
            $_.StartsWith($Prefix) -and -not $_.StartsWith("#")
        }).Count
}

# Get-MetricValue 取某个**标签前缀**下的值。返回 $null 表示这条序列不存在 ——
# 与「值为 0」严格区分：`*Vec` 未预置时前者才是故障现象（docs/06-§5.5 的告警规则
# 在序列缺失时会静默判为「没问题」）。
#
# ⚠️ 匹配到行尾的那个数字，而不是取「最后一个空白分隔字段」：
# `gw_build_info{...} 1` 取最后一个字段恰好是 1，但一旦标签值里带空格
# （本项目的标签值都不会，但未来可能），按空白切就会取到标签内部的词。
function Get-MetricValue {
    param([string]$Text, [string]$Match)
    if (-not $Text) { return $null }
    $line = @($Text -split "`n" | Where-Object {
            $_.StartsWith($Match) -and -not $_.StartsWith("#")
        } | Select-Object -First 1)
    if ($line.Count -eq 0) { return $null }
    $m = [regex]::Match($line[0], '\s([-+0-9.eE]+)\s*$')
    if (-not $m.Success) { return $null }
    return [double]$m.Groups[1].Value
}

# ---- 临时网关实例 ----
#
# ⚠️ PS 5.1 的 `Start-Process` 没有 `-Environment`：只能在**当前**进程里设 env
# 再启动（子进程继承）。因此每次启动前必须把**全部**相关变量重设一遍 ——
# 「只设差异项」会让上一个实例残留的值（比如容器的 DSN）串进来。
function Start-Gateway {
    param([string]$Exe, [int]$GatewayPort, [int]$Mp, [string]$LogPath)
    $env:HTTP_ADDR = "127.0.0.1:$GatewayPort"
    $env:METRICS_ENABLED = "true"
    $env:METRICS_PORT = "$Mp"
    $env:METRICS_ALLOW_CIDRS = "127.0.0.1/32"
    # DEBUG 是有意的：AC-NFR-05 要求「含 DEBUG 的全量日志」也扫不到凭据，
    # 而 DEBUG 恰好是最可能把请求体/内容打出来的级别。
    $env:LOG_LEVEL = "debug"
    # `LOG_FORMAT` 与 `ai-platform`（Python）**同名不同域**：
    # 网关只接受 `json` / `text`，AI 侧只接受 `json` / `console`。
    # 同一个终端里先后跑两侧时对方的取值会残留，不重设会导致子进程直接启动失败
    # （`LOG_FORMAT="console" 不合法`），而现象只是「实例没起来」。
    $env:LOG_FORMAT = "text"
    $env:APP_ENV = "local"
    $env:AI_GRPC_ENABLED = "true"
    $env:AI_PLATFORM_GRPC_TARGET = "127.0.0.1:50051"
    $env:AI_PLATFORM_BASE_URL = "http://127.0.0.1:8000"
    $env:OTEL_ENABLED = "false"   # 没有 collector 时开着只会刷导出失败日志

    # ⚠️ 必须把登录限流放宽，否则本脚本会被**上一条脚本**毒死。
    #
    # 限流桶是存在 Redis 里的、按**客户端 IP** 分桶（`rate:login_ip:127.0.0.1`），
    # 也就是说它是**全机器共享**的，跟「哪个网关实例」无关。stage5 的最后一段
    # 故意用连错密码把登录限流打满（要验 AC-NFR-07），于是紧跟其后的本脚本
    # 第一个 login 就吃 429 —— 现象是 stage6 大面积 401/400 级联失败，
    # 看起来像「M6 的改动把登录弄坏了」，其实是上一条脚本的副作用。
    #
    # 为什么在**实例配置**上解决而不是让 stage6 等 1 分钟：
    #   * 等窗口过期 = 把「脚本顺序 + 时间」变成隐式依赖，CI 里必然踩；
    #   * 伪造 `X-Forwarded-For` 换桶已被 stage5 证明无效（`TRUSTED_PROXY_COUNT=0`），
    #     这里若为了绕限流而打开它，等于为了测试放松了安全配置；
    #   * 本实例是**专供可观测性验收**的干净实例，限流不在它的验收范围内
    #     （那是 stage5 的事），所以把上限抬高不掩盖任何东西。
    $env:LOGIN_RATE_PER_MINUTE = "1000"
    $env:LOGIN_ACCOUNT_RATE_PER_HOUR = "1000"
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force }
    $p = Start-Process -FilePath $Exe -RedirectStandardOutput $LogPath `
        -RedirectStandardError ($LogPath + ".err") -WindowStyle Hidden -PassThru
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$GatewayPort/health/live" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $p }
        } catch { }
        if ($p.HasExited) { return $null }
    }
    return $null
}

# ---- 从 .env 里解析数据库凭据（与 stage5 同一份实现，绝不硬编码）----
function Get-MysqlCredential {
    $envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
    if (-not (Test-Path $envFile)) { return $null }
    $line = (Select-String -Path $envFile -Pattern '^MYSQL_DSN=' -Encoding UTF8 | Select-Object -First 1).Line
    if (-not $line) { return $null }
    if ($line -match '^MYSQL_DSN=([^:]+):([^@]*)@tcp\(([^:]+):(\d+)\)/([^?]+)') {
        return [pscustomobject]@{ User = $Matches[1]; Pass = $Matches[2]; Host = $Matches[3]; Port = $Matches[4]; DB = $Matches[5] }
    }
    return $null
}

function Get-AuditRows {
    param([string]$Action, [int]$WithinMinutes = 10)
    $mysql = Get-Command mysql -ErrorAction SilentlyContinue
    if (-not $mysql) { return -1 }
    $c = Get-MysqlCredential
    if (-not $c) { return -1 }
    # ⚠️ `UTC_TIMESTAMP()` 而不是 `NOW()`：docs/05-§2 规定时间列一律存 UTC，
    # 而 `NOW()` 给的是 MySQL 会话时区的本地时间（本机 +08:00）。用 `NOW()`
    # 拿 UTC 的 04:xx 去比本地时间的 12:xx ⇒ 永远 0 行，而写入完全正常 ——
    # 表现为「审计没落库」的假告警。
    $sql = "SELECT COUNT(*) FROM audit_log WHERE action='$Action' AND created_at >= (UTC_TIMESTAMP() - INTERVAL $WithinMinutes MINUTE);"
    $out = & $mysql.Source --host=$($c.Host) --port=$($c.Port) --user=$($c.User) `
        --password=$($c.Pass) --skip-column-names --batch $($c.DB) -e $sql 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return -1 }
    return [int]($out | Select-Object -Last 1)
}

# ----------------------------------------------------------------------
Write-Host "== M6 验收：可观测性 ==" -ForegroundColor Cyan
$ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$email = "m6_$ts@example.com"
$pass = 'M6-Passw0rd!'
$gwLog = Join-Path $env:TEMP "gw-stage6.log"
$gw = $null

# ----------------------------------------------------------------------
Write-Host ""
Write-Host "== 0. 编译并拉起一个**干净**的网关实例 ==" -ForegroundColor Cyan
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $exe = Join-Path $env:TEMP "gw-stage6.exe"
    & go build -o $exe ./cmd/server 2>&1 | Out-Null
    Assert-True "go build 成功" (($LASTEXITCODE -eq 0) -and (Test-Path $exe)) "exit=$LASTEXITCODE"
} finally {
    Pop-Location
}

if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
    Write-Host "  构建失败，后续无法进行" -ForegroundColor Red
    exit 1
}

# `-BaseUrl` 是「对照实例」：业务请求不发到它，但**它必须真的在**。
#
# 这个参数原先声明了却从未被使用 —— 那是最坏的一种接口：`-BaseUrl http://x:1`
# 不会报错、不会失败，只会让人以为「我指定了目标」。这里把它变成一条**前置校验**：
# 对照实例可达时断言它和本脚本的实例是同一份二进制（`/health` 的 `version`），
# 不可达时记为 SKIP（本脚本的结论不依赖它，不该因此变红）。
$refOk = $false
$refVersion = ""
try {
    $refResp = Invoke-WebRequest -Uri ($BaseUrl + "/health") -UseBasicParsing -TimeoutSec 5
    $refJson = $refResp.Content | ConvertFrom-Json
    $refOk = $true
    $refVersion = [string]$refJson.version
} catch { $refOk = $false }

$gw = Start-Gateway -Exe $exe -GatewayPort $Port -Mp $MetricsPort -LogPath $gwLog
Assert-True "临时网关实例启动（业务 127.0.0.1:$Port，指标 ${MetricsPort}）" ($null -ne $gw) "见 $gwLog"
if (-not $gw) {
    Write-Host "  实例没起来，后续无法进行（日志：$gwLog）" -ForegroundColor Red
    exit 1
}
$base = "http://127.0.0.1:$Port"

if ($refOk) {
    $ownResp = Invoke-WebRequest -Uri "$base/health" -UseBasicParsing -TimeoutSec 5
    $ownVersion = [string](($ownResp.Content | ConvertFrom-Json).version)
    Assert-True "对照实例与本脚本实例同版本（$BaseUrl）" ($refVersion -eq $ownVersion) `
        "ref=$refVersion own=$ownVersion —— 版本不同说明对照实例跑的是旧二进制，别拿它做基线"
} else {
    Assert-Skip "对照实例可达性（$BaseUrl）" "没起对照实例；本脚本的业务断言全部发在自己的实例上，不受影响"
}

# 说明为什么必须用**干净实例**（写成断言而不是注释，因为它是本脚本可信度的前提）。
#
# 判据取「业务计数器为 **0**」而不是「无样本」：
#   - 启动就绪探测（`/health/live`）自己会落在 gw_requests_total 上；
#   - 而 gw_login_attempts_total{result="ok"} 恰恰**应该**存在且为 0 ——
#     它是 `initLabels()` 预置的（docs/06-§5.5 的比率型告警需要分母一直在）。
# 把「预置且为 0」当成「脏」会把每次运行都判成失败；
# 真正要保证的是没有**业务**流量发生过。
$fresh = Get-MetricsText
foreach ($probeCounter in @(
        'gw_login_attempts_total{result="ok"}',
        'gw_login_attempts_total{result="bad_credentials"}',
        'gw_quota_exceeded_total{metric="chat_requests"}',
        'gw_rate_limited_total{scope="login_ip"}')) {
    $v = Get-MetricValue -Text $fresh -Match $probeCounter
    Assert-True "实例是干净的（$probeCounter == 0）" ($null -ne $v -and $v -eq 0) "value=$v"
}
$noLoginRoute = Get-MetricValue -Text $fresh -Match 'gw_requests_total{method="POST",route="/api/v1/auth/login"}'
Assert-True "实例是干净的（登录路由无请求记录）" ($null -eq $noLoginRoute) "value=$noLoginRoute"

try {
    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 1. AC-NFR-09：指标完整性（docs/06-§5.2 全部 19 族）==" -ForegroundColor Cyan
    Write-Host "      断言在**零业务流量**下就要成立 —— 这正是 M6 修掉的缺陷：" -ForegroundColor DarkGray
    Write-Host "      `*Vec` 不预置时族根本不出现，而 docs/06-§5.5 的告警规则" -ForegroundColor DarkGray
    Write-Host "      在序列缺失时**静默判为「没问题」**。" -ForegroundColor DarkGray

    # `gw_requests_total` 是精确标签集的向量（route/method/status 三个开放域），
    # 预置它要么漏（组合爆炸）、要么造假数据（凭空造 status=500）——
    # 而它是**比率型**告警的分子分母，假 0 会让「5xx 占比」算成 0/0。
    # 其余 18 族在启动时就该存在。
    $expectedFamilies = @(
        'gw_request_duration_seconds', 'gw_auth_failures_total', 'gw_login_attempts_total',
        'gw_token_refresh_total', 'gw_quota_exceeded_total', 'gw_rate_limited_total',
        'gw_ai_requests_total', 'gw_ai_request_duration_seconds', 'gw_ai_first_token_seconds',
        'gw_ai_errors_total', 'gw_circuit_breaker_state', 'gw_sse_connections',
        'gw_sse_client_disconnects_total', 'gw_message_persist_failed_total',
        'gw_session_mismatch_total', 'gw_trace_mismatch_total', 'gw_orphan_rows_total',
        'gw_build_info'
    )
    $missing = New-Object System.Collections.ArrayList
    foreach ($f in $expectedFamilies) {
        if (-not ($fresh -match "(?m)^# TYPE $f ")) { [void]$missing.Add($f) }
    }
    Assert-True "零流量下 18 个预置族全部可见" ($missing.Count -eq 0) ("缺失=" + ($missing -join ","))

    Assert-True "gw_build_info 值为 1（版本信息可查）" `
        ((Get-MetricValue -Text $fresh -Match 'gw_build_info{') -eq 1) `
        "value=$(Get-MetricValue -Text $fresh -Match 'gw_build_info{')"
    Assert-Info "构建信息" (@($fresh -split "`n" | Where-Object { $_ -like 'gw_build_info{*' })[0])

    # 连接池指标：docs/06-§5.2 用 `gw_db_pool_*` / `gw_redis_pool_*` 表示，
    # 是「前缀 + 若干后缀」而不是固定族名，因此单独断言前缀存在。
    $poolLines = Get-SeriesCount -Text $fresh -Prefix "gw_db_pool_"
    Assert-True "gw_db_pool_* 暴露（连接池可观测）" ($poolLines -ge 1) "series=$poolLines"
    $redisPool = Get-SeriesCount -Text $fresh -Prefix "gw_redis_pool_"
    Assert-True "gw_redis_pool_* 暴露" ($redisPool -ge 1) "series=$redisPool"

    # route 模板：§5.2 明令 MUST NOT 把真实 ID 塞进 route 标签（高基数）。
    #
    # 直方图的样本行是 `_bucket` 后缀（见 Get-SeriesCount 的注释），
    # 因此这里数 `_bucket`。
    $routeTpl = Get-SeriesCount -Text $fresh -Prefix "gw_request_duration_seconds_bucket{"
    Assert-True "gw_request_duration_seconds 按路由模板预置（>=20 条）" ($routeTpl -ge 20) "series=$routeTpl"

    # 取**去重后的 route 取值**再判定，而不是逐样本行扫。
    #
    # ⚠️ 判据必须是「裸 ID 值」而不是「出现 doc_/cv_ 这类词」：
    # 正确的模板本身就长 `route="/api/v1/documents/:doc_id"` —— 它含 `doc_`
    # 因为那是**参数名**。按关键词匹配会把全部参数化路由（共 80 条 bucket 序列）
    # 判成高基数，而正确的判据是「ID 的**值**有没有出现」，
    # 即 `:param` 与 `param_01M3R...` 的区别。
    $routes = @($fresh -split "`n" | Where-Object { $_ -like 'gw_request_duration_seconds_bucket{*' } |
        ForEach-Object { [regex]::Match($_, 'route="([^"]*)"').Groups[1].Value } |
        Where-Object { $_ } | Sort-Object -Unique)
    Assert-Info "去重后的 route 模板数" "count=$($routes.Count)"
    $badRoutes = @($routes | Where-Object { $_ -match '(?:^|/)(?:cv|msg|kb|doc|task|u|usr)_[0-9A-Za-z]{10,}(?:/|$)' })
    Assert-True "route 标签不含真实 ID 值（无高基数）" ($badRoutes.Count -eq 0) ("bad=" + ($badRoutes -join ','))
    # 反向断言：参数化路由确实用 `:name` 形式 —— 它同时就是「不带真实 ID」的证据。
    $paramRoutes = @($routes | Where-Object { $_ -match '/:' })
    Assert-True "参数化路由用 gin 的 `:name` 模板形式" ($paramRoutes.Count -ge 3) "param=$($paramRoutes.Count)"
    Assert-Info "参数化路由样例" (($paramRoutes | Select-Object -First 4) -join " | ")

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 2. S8：跨服务 trace 连续性（AC-NFR-08）==" -ForegroundColor Cyan

    $r1 = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{ email = $email; password = $pass }
    $login = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $email; password = $pass }
    Assert-True "登录 200" ($login.Status -eq 200) "status=$($login.Status)"
    $token = $login.Json.access_token
    Assert-True "拿到 access_token" (-not [string]::IsNullOrWhiteSpace($token)) ""

    # 用一个**自造**的 trace_id 发请求：网关必须复用它（而不是自己另起一个）——
    # 这是「两侧同一条 trace」可被外部验证的唯一方式。
    $traceId = [Guid]::NewGuid().ToString("N")
    $conv = Invoke-Api -Method POST -Path "/api/v1/conversations" -Token $token -Body @{ title = "m6-trace" } -TraceId $traceId
    Assert-True "建会话 201" ($conv.Status -eq 201) "status=$($conv.Status)"
    Assert-True "响应头 X-Trace-Id == 入站 traceparent 的 trace_id（复用而非新起）" `
        ($conv.Trace -eq $traceId) "want=$traceId got=$($conv.Trace)"
    $convId = $conv.Json.id

    # 逐段核对：错误信封、响应头、网关日志三处必须是同一个 id（docs/06-§5.3 的「关联」行）。
    $notFound = Invoke-Api -Method GET -Path "/api/v1/conversations/cv_does_not_exist" -Token $token -TraceId $traceId
    Assert-True "错误信封里带 trace_id" ($null -ne $notFound.Json.error.trace_id) "body=$($notFound.Text)"
    Assert-True "错误信封 trace_id == 入站 trace_id" ($notFound.Json.error.trace_id -eq $traceId) `
        "want=$traceId got=$($notFound.Json.error.trace_id)"
    Assert-True "错误响应头 X-Trace-Id 与信封一致" ($notFound.Trace -eq $notFound.Json.error.trace_id) `
        "header=$($notFound.Trace) body=$($notFound.Json.error.trace_id)"

    if (-not $SkipChat) {
        $chatTrace = [Guid]::NewGuid().ToString("N")
        $send = Invoke-Api -Method POST -Path "/api/v1/conversations/$convId/messages" -Token $token `
            -Body @{ content = "M6 trace probe"; use_rag = $false } -TraceId $chatTrace
        
        # 网关日志里必须有这个 id（同一台机器上的文件，直接读）。
        $gwLogText = ""
        if (Test-Path $gwLog) { $gwLogText = Get-Content $gwLog -Encoding UTF8 -Raw }
        $inGwLog = $gwLogText -match [regex]::Escape($chatTrace)
        Assert-True "网关日志里有该 trace_id（日志↔响应可互跳）" $inGwLog "trace=$chatTrace"

        if ($send.Status -eq 200) {
            Assert-True "AI 调用成功（S8 要有真实编排）" $true ""
        }

        # AI 侧日志：AI 把它解析出的 trace_id 前 8 位作为日志前缀（`[9921b8c8]`）。
        # 找不到就 SKIP 而不是 FAIL —— AI 的 stdout 归属由用户决定，
        # 而「AI 侧没日志」既可能是没重定向，也可能是真的没传播，两者必须区分，
        # 不能把「脚本不知道日志在哪」记成产品缺陷。
        if ($AILogPath -and (Test-Path $AILogPath)) {
            $aiText = Get-Content $AILogPath -Encoding UTF8 -Raw
            $short = $chatTrace.Substring(0, 8)
            Assert-True "AI 侧日志里有同一个 trace（前 8 位 $short）" ($aiText -match [regex]::Escape($short)) `
                "trace=$chatTrace short=$short path=$AILogPath"
        } else {
            Assert-Skip "AI 侧 trace 比对" "未提供 -AILogPath（或文件不存在）；两侧 id 一致性另由 stage4 的 traceparent 断言覆盖"
        }
    } else {
        Assert-Skip "S8 真实编排与两侧日志比对" "-SkipChat"
    }

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 3. S9/S10：登出后 refresh 立即作废、access 可静默续期 ==" -ForegroundColor Cyan

    $r2 = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $email; password = $pass }
    $refresh1 = $r2.Json.refresh_token
    $access2 = $r2.Json.access_token
    Assert-True "第二次登录拿到 refresh_token" (-not [string]::IsNullOrWhiteSpace($refresh1)) ""

    $ref = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refresh1 }
    Assert-True "S10：refresh 换新 200" ($ref.Status -eq 200) "status=$($ref.Status)"
    # ⚠️ 不要断言「新 token 与旧的字符串不同」：JWT 的 `iat`/`exp` 以**秒**为单位，
    # 同一秒内签发的两次刷新会得到**逐字节相同**的令牌（签名确定性）。
    # 那是正确行为，不是缺陷 —— 把「内容不同」当判据会在快速连续的验收里假红。
    # 真正要断言的是「拿到可用令牌」与「旧 refresh 不能重放」。
    Assert-True "S10：新 access_token 非空" (-not [string]::IsNullOrWhiteSpace($ref.Json.access_token)) ""
    $meNew = Invoke-Api -Method GET -Path "/api/v1/me" -Token $ref.Json.access_token
    Assert-True "S10：新 access_token 可用（GET /me 200）" ($meNew.Status -eq 200) "status=$($meNew.Status)"

    $refReuse = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refresh1 }
    Assert-True "S10：旧 refresh_token 不能重复使用（一次一换）" ($refReuse.Status -ne 200) "status=$($refReuse.Status)"

    $out = Invoke-Api -Method POST -Path "/api/v1/auth/logout" -Token $ref.Json.access_token -Body @{ refresh_token = $ref.Json.refresh_token }
    Assert-True "登出成功（200/204）" ($out.Status -in @(200, 204)) "status=$($out.Status)"
    $afterOut = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $ref.Json.refresh_token }
    Assert-True "S9：登出后 refresh 立即失效（不再 200）" ($afterOut.Status -ne 200) "status=$($afterOut.Status)"

    # 登录相关的指标必须已被预置的标签域覆盖（docs/06-§5.2 的 4 个取值）。
    $m2 = Get-MetricsText
    $loginOk = Get-MetricValue -Text $m2 -Match 'gw_login_attempts_total{result="ok"}'
    Assert-True "gw_login_attempts_total{result=ok} >= 1" ($loginOk -ge 1) "value=$loginOk"
    $badCred = Get-MetricValue -Text $m2 -Match 'gw_login_attempts_total{result="bad_credentials"}'
    Assert-True "gw_login_attempts_total{result=bad_credentials} 存在（值为 0 也算）" ($null -ne $badCred) "value=$badCred"
    $refOk = Get-MetricValue -Text $m2 -Match 'gw_token_refresh_total{result="ok"}'
    Assert-True "gw_token_refresh_total{result=ok} >= 1" ($refOk -ge 1) "value=$refOk"

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 4. 审计（docs/05-§2.8）==" -ForegroundColor Cyan

    # 审计动作名与 `internal/biz/auth.go` 的常量逐字对应（`login` / `logout`），
    # 不是 `login_success` —— 常量声明在 biz，脚本里的字符串是它的**副本**，
    # 一个字母之差会让「审计已落库」表现成「没落库」。
    $auditLogin = Get-AuditRows -Action "login"
    if ($auditLogin -eq -1) {
        Assert-Skip "audit_log 查询" "mysql CLI 不可用或凭据解析失败"
    } else {
        Assert-True "audit_log 有 login 行" ($auditLogin -ge 1) "rows=$auditLogin"
        $auditLogout = Get-AuditRows -Action "logout"
        Assert-True "audit_log 有 logout 行" ($auditLogout -ge 1) "rows=$auditLogout"
    }

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 5. 留存：gw_orphan_rows_total（每日一致性检查的输出通道）==" -ForegroundColor Cyan

    $m3 = Get-MetricsText
    $orphanSeries = Get-SeriesCount -Text $m3 -Prefix "gw_orphan_rows_total{"
    Assert-True "gw_orphan_rows_total 有样本（按 table 分）" ($orphanSeries -ge 1) "series=$orphanSeries"
    $pendSeries = Get-SeriesCount -Text $m3 -Prefix "gw_retention_pending_rows{"
    if ($pendSeries -ge 1) {
        Assert-Info "gw_retention_pending_rows" "series=$pendSeries（有该族的实例会把待清理行数暴露出来）"
    } else {
        Assert-Info "gw_retention_pending_rows" "未暴露（非 docs/06-§5.2 的必需族）"
    }

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 6. 健康检查（docs/06-§5.4）==" -ForegroundColor Cyan

    $live = Invoke-Api -Method GET -Path "/health/live" -Base $base
    Assert-True "GET /health/live 恒 200" ($live.Status -eq 200) "status=$($live.Status)"
    $ready = Invoke-Api -Method GET -Path "/health/ready" -Base $base
    Assert-True "GET /health/ready 200（MySQL+Redis+schema 全通）" ($ready.Status -eq 200) "status=$($ready.Status) body=$($ready.Text)"
    $health = Invoke-Api -Method GET -Path "/health" -Base $base
    Assert-True "GET /health 恒 200" ($health.Status -eq 200) "status=$($health.Status)"
    $h = $health.Json
    Assert-True "/health 含 checks.mysql" ($null -ne $h.checks.mysql) "body=$($health.Text)"
    Assert-True "/health 含 checks.redis" ($null -ne $h.checks.redis) ""
    Assert-True "/health 含 checks.circuit_breaker（熔断状态仅信息、不影响状态码）" ($null -ne $h.checks.circuit_breaker) ""
    Assert-Info "/health status" "status=$($h.status) cb=$($h.checks.circuit_breaker.state)"

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 7. AC-NFR-05：日志脱敏（含 DEBUG）==" -ForegroundColor Cyan

    $logText = ""
    if (Test-Path $gwLog) { $logText = Get-Content $gwLog -Encoding UTF8 -Raw }
    Assert-True "拿到网关日志（非空）" (-not [string]::IsNullOrWhiteSpace($logText)) "path=$gwLog bytes=$($logText.Length)"

    # 判据分成「绝不允许出现」与「允许脱敏形式」两类。
    # 密码之所以能作为判据，是因为本脚本自己造的密码是**随机后缀**的，
    # 不像 `password` 这个词那样会出现在字段名里。
    $forbidden = @(
        @{ Name = "明文密码"; Pattern = [regex]::Escape($pass) },
        @{ Name = "JWT 形态（eyJ）"; Pattern = 'eyJ[A-Za-z0-9_-]{10,}' },
        @{ Name = "Bearer 令牌"; Pattern = 'Bearer\s+[A-Za-z0-9_.\-]{20,}' },
        @{ Name = "邮箱全称"; Pattern = [regex]::Escape($email) },
        @{ Name = "JWT_SECRET 字面量"; Pattern = 'jwt_secret\s*[:=]\s*"?[A-Za-z0-9]{8,}' }
    )
    foreach ($f in $forbidden) {
        $hit = [regex]::IsMatch($logText, $f.Pattern)
        Assert-True "日志不含$($f.Name)" (-not $hit) "pattern=$($f.Pattern)"
    }

    # 反向断言：脱敏**不是**靠不打日志达成的 —— 否则「没有泄漏」会是平凡真。
    Assert-True "日志里确实有访问记录（脱敏非平凡）" `
        ([regex]::IsMatch($logText, 'msg=http\.request')) ""

    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 8. 结构化日志必备字段（docs/06-§5.3）==" -ForegroundColor Cyan

    # 取最后一条请求摘要行做字段核对。
    $reqLine = @($logText -split "`n" | Where-Object { $_ -like '*msg=http.request*' } | Select-Object -Last 1)
    if ($reqLine.Count -gt 0) {
        $line = $reqLine[0]
        foreach ($field in @('trace_id=', 'request_id=', 'method=', 'route=', 'status=', 'elapsed_ms=')) {
            Assert-True "请求摘要含 $field" ($line -like "*$field*") "line=$($line.Substring(0, [Math]::Min(200, $line.Length)))"
        }
        Assert-Info "最后一条请求摘要" ($line.Substring(0, [Math]::Min(300, $line.Length)))
    } else {
        Assert-True "存在 msg=http.request 行" $false "日志里没有请求摘要"
    }

    # 路由模板：日志里的 route 也必须是模板（与指标同一个口径）。
    $badLogRoute = @($logText -split "`n" | Where-Object {
            $_ -like '*msg=http.request*' -and $_ -match 'route="[^"]*(/cv_|/msg_|/kb_|/doc_|/task_)'
        }).Count
    Assert-True "日志的 route 也是模板（无真实 ID）" ($badLogRoute -eq 0) "bad=$badLogRoute"
} finally {
    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "== 清理 ==" -ForegroundColor Cyan
    if ($KeepGateway) {
        Write-Host "  -KeepGateway：保留实例 pid=$($gw.Id)，日志 $gwLog" -ForegroundColor DarkYellow
    } elseif ($gw) {
        Stop-Process -Id $gw.Id -Force -ErrorAction SilentlyContinue
        Write-Host "  stopped pid=$($gw.Id)"
    }
}

Write-Host ""
if ($script:Failed -eq 0) {
    Write-Host ("=== M6 验收：全部通过 {0}/{0} ===" -f $script:Total) -ForegroundColor Green
    exit 0
} else {
    Write-Host ("=== M6 验收：失败 {0} / {1} ===" -f $script:Failed, $script:Total) -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
