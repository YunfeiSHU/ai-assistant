# =============================================================================
# 一次跑完全部六阶段验收，并汇总成一张表。
#
# ## 为什么需要这个脚本
#
# 六条脚本**不是顺序无关的**，而「A 全绿 / B 全红」在本项目里出现过多次，
# 其中一部分**不是产品缺陷，而是脚本之间通过共享全局状态互相污染**。
# 逐条手跑很容易把它误判成「M6 的改动把登录弄坏了」。
#
# 本脚本把那层污染**显式地**处理掉，手段分两类：
#
# ### ① 按实例隔离（能隔离的都隔离）
#   每个阶段都用**专用网关实例**，且启动前把**全部**相关 env 重设一遍
#   （PS 5.1 的 `Start-Process` 没有 `-Environment`，残留值会串进子进程）：
#
#     | 实例 | 端口 / 指标 | 限流档案 | 配额套餐 | 服务于 |
#     | --- | --- | --- | --- | --- |
#     | A | 18085 / 9121 | login/msg/conv/upload 全部 1000/min | 放宽（`chat_requests` 100 万等） | M1、M2、M3、M4、M6 |
#     | B | 18086 / 9122 | **空 = 全走生产默认** | **空 = 全走生产默认** | M5（唯一断言限流/配额的阶段） |
#
#   为什么要两套限流：`M1` 一条脚本就有 10 个登录点，正好等于默认上限；M1–M4、M6
#   从不断言限流（`grep 429` 命中 0），却会被限流打死。而 M5 恰恰要断言
#   「连续错误登录会出现 429」，所以它**必须**跑在默认上限的实例上。
#
#   ⚠️ 放宽的范围必须**覆盖全部限流键**，不能只放宽 login。曾经只放宽 login，
#   结果 M2 用同一个用户发了 **23** 条消息，撞上默认 `MSG_RATE_PER_MINUTE=20`，
#   第 21 条起 429，报成
#   `I6 sending to a deleted conversation -> 404 :: expected=404 actual=429`。
#   消息桶 `RateLimitByUser(..., RateScopeMessage)` 是 **per user** 的，跟打哪个
#   实例无关 ⇒ 换实例救不了，只能放宽上限。判据：**429 出现在一条从不提 429 的
#   脚本里**就是污染，不是缺陷（该脚本的期望值不可能因为产品改动变成 429）。
#
#   ⚠️ 与限流**并列**的第二层「按日累计」污染：配额（`QUOTA_PLAN_MAP`）。
#   验收脚本用的是**固定 user_id**，而 `chat_requests` 是**按自然日**累计的
#   （`DefaultPlanLimits()` 的 free 档只有 100/日）⇒「今天第 N 次跑全套」会从
#   某一条断言起**全部**变成 `429 QUOTA_EXCEEDED`。它的表象和限流一模一样，
#   但**成因完全不同**（一个是时间窗，一个是日累计），排查方向也完全不同：
#   看网关日志的 `code=` 是 `QUOTA_EXCEEDED` 还是 `RATE_LIMITED`。
#   实测：实例 A 日志里 16 条 `QUOTA_EXCEEDED`，M2 的 G 段 5 并发只成功 1 条。
#   ⇒ 非限流实例同样必须放宽配额套餐。
#
# ### ② 等待窗口过期（唯一无法靠实例隔离的共享状态）
#   `login_ip` 限流桶是 `gw:rate:login_ip:<IP>:<bucket>` 存在 **Redis** 里，
#   **按客户端 IP 分桶 ⇒ 跨实例共享**。本地跑时 IP 恒为 127.0.0.1，
#   所以「换实例」根本换不出一份新配额 —— 这是本地多实例验收最容易搞错的一点。
#   于是 M5 开跑前必须等窗口（60s）从零开始，否则 M5 自己的 register/login
#   就会吃 429，级联出一屏假失败（曾实测 8 条：`A2 login as stage1 -> 429` 之后
#   全是 401，看起来像鉴权坏了）。
#
#   不盲睡 60s：用「探一次坏登录，看还 429 吗」来判断窗口是否已经翻过。
#   探测本身会占掉 1 次额度，但 M5 后面的段落只发 1 次登录，10 的额度够用。
#
# ### ③ 等 AI 侧排空（既没有共享状态也没有 429 的那种污染）
#   M5 的 8MB 上传会在 AI 侧留下几万个 chunk 的**后台 CPU 向量化**（单事件循环）。
#   紧接着再跑一遍时机器整体慢 2~3 倍，M4 的两条**时序**断言就假红。
#   开跑前读网关 `/health` 的 `checks.ai_platform.latency_ms`（空闲十几毫秒、
#   后台忙时几百毫秒），连续 3 次低于阈值才开跑。判据见 `Wait-AIDrain` 注释。
#
# ## 两个环境相关的坑（都会表现成「产品坏了」）
#   * `Invoke-WebRequest` 在本机会走系统代理，对 `127.0.0.1` 也可能超时
#     （实测同一请求：IWR 10s 超时 / `curl.exe --noproxy '*'` 412ms）。
#     所以本脚本所有探测都走 `curl.exe --noproxy '*'`。
#   * `-SkipChat` 会跳过真实模型调用，M3/M4 的断言数随之下降，
#     下限要用 `SkipMin`（见阶段表注释）。
#
# ## 前置（三个进程缺一不可）
#   1) AI gRPC：`$env:GRPC_ENABLED="true"; uv run python -m app.grpc`     （50051）
#   2) AI HTTP：`$env:METRICS_PORT="9106";      uv run python -m app.main` （8000）
#   3) 本脚本**自己**拉起 A/B 两个网关实例（不再要求你手动起 18080）
#
# ## 用法
#   powershell -ExecutionPolicy Bypass -File tools\run_all_stages.ps1
#   powershell -ExecutionPolicy Bypass -File tools\run_all_stages.ps1 -SkipChat     # 不跑真实模型
#   powershell -ExecutionPolicy Bypass -File tools\run_all_stages.ps1 -KeepGateway  # 留着实例便于排查
#   （整套连跑约 8~10 分钟；M5 的 8MB 上传占其中 1.5~4 分钟）
# =============================================================================

[CmdletBinding()]
param(
    # 阶段实例（限流全部放宽，见 `$stageRateEnv`）：M1–M4、M6 用。
    [int]$StagePort = 18085,
    [int]$StageMetricsPort = 9121,
    # 限流实例（全走生产默认）：只有 M5 用。
    [int]$LimitPort = 18086,
    [int]$LimitMetricsPort = 9122,
    # AI 侧地址（三个进程里的两个）。
    [string]$AIBaseUrl = "http://127.0.0.1:8000",
    [string]$AIGrpcTarget = "127.0.0.1:50051",
    # AI 健康探测往返超过这个值就认为「AI 侧后台还在忙」，等它降到阈值以下再开跑。
    # 空闲实测十几毫秒（首帧另有约 300ms 建连预热），后台向量化时是几百毫秒。
    [int]$AIDrainMaxMs = 500,
    [switch]$SkipChat,
    [switch]$KeepGateway
)

$ErrorActionPreference = 'Continue'
# PS 5.1 默认按 gb2312 解码子进程输出，而脚本与网关都输出 UTF-8：
# 不设这两行，落盘/回显里的中文会是乱码。
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repo = Split-Path $PSScriptRoot -Parent
$exe = Join-Path $env:TEMP "gw-allstages.exe"

$script:Started = New-Object System.Collections.ArrayList

# ---- 启动一个专用网关实例 -------------------------------------------------
#
# ⚠️ **必须把每个用到的 env 都显式重设**。「只设差异项」是这里最容易犯的错：
# 上一次实例残留的 `LOGIN_RATE_PER_MINUTE` / `AI_PLATFORM_*` 会串进下一个，
# 而症状只是「断言莫名失败」。
#
# `-RateEnv` 是「限流档案」：一张 `键 => 值` 表，函数会先把**全部**限流键清成生产
# 默认，再按表覆盖。不写成零散的 `-XxxPerMinute` 参数、也不写成「只设差异项」，
# 原因是限流桶**按用户/IP 存在 Redis 里**，跟实例无关 —— 谁的桶被填满，下一个
# 打到同一个桶的脚本就挨 429。零散参数会让「漏设一个键」变成静默的跨阶段污染。
function Start-StageGateway {
    param([int]$Port, [int]$Mp, [hashtable]$RateEnv, [string]$PlanMap, [string]$Name)
    $env:HTTP_ADDR = "127.0.0.1:$Port"
    $env:METRICS_ENABLED = "true"
    $env:METRICS_PORT = "$Mp"
    $env:METRICS_ALLOW_CIDRS = "127.0.0.1/32"
    $env:APP_ENV = "local"
    # `LOG_FORMAT` 与 `ai-platform`（Python）**同名不同域**：网关只接受
    # `json` / `text`，AI 侧只接受 `json` / `console`。同一终端里先后跑两侧时
    # 对方的取值会残留，不重设会让子进程**直接启动失败**（`LOG_FORMAT="console" 不合法`），
    # 而现象只是「实例没起来」。
    $env:LOG_FORMAT = "text"
    $env:LOG_LEVEL = "info"
    $env:AI_GRPC_ENABLED = "true"
    $env:AI_PLATFORM_GRPC_TARGET = $AIGrpcTarget
    $env:AI_PLATFORM_BASE_URL = $AIBaseUrl
    $env:OTEL_ENABLED = "false"
    # 先把所有限流键**清掉**（= 回落到 `internal/conf/load.go` 的生产默认），
    # 再按档案覆盖。清 + 设是两步，不能只做后一步：不清就会把上一个实例的值
    # 带过来，而 `Start-Process` 继承的是**本进程**的环境块（没有 `-Environment` 参数）。
    foreach ($k in @(
            "LOGIN_RATE_PER_MINUTE", "LOGIN_ACCOUNT_RATE_PER_HOUR",
            "MSG_RATE_PER_MINUTE", "CONV_RATE_PER_MINUTE", "UPLOAD_RATE_PER_MINUTE")) {
        Remove-Item ("env:" + $k) -ErrorAction SilentlyContinue
    }
    foreach ($k in $RateEnv.Keys) { Set-Item ("env:" + $k) $RateEnv[$k] }
    # 配额套餐同理，而且这一条踩过**更难查**的形态：
    # `DefaultPlanLimits()` 的 free 档是 `chat_requests = 100/日`，而验收脚本用的是
    # **固定 user_id**（`u_test_stage2` 之类），日额度又存在 Redis 里按自然日累计 ——
    # 于是「今天第 N 次跑全套」会从某一条断言开始**全部**变成
    # `429 QUOTA_EXCEEDED`，看起来像「限流坏了」，其实与限流无关。
    # 实测：实例 A 的日志里 16 条 `code=QUOTA_EXCEEDED`，M2 的 G 段 5 并发只成功 1 条。
    # 所以非限流实例必须放宽（`-PlanMap`），限流实例留空走默认。
    Remove-Item "env:QUOTA_PLAN_MAP" -ErrorAction SilentlyContinue
    if ($PlanMap) { $env:QUOTA_PLAN_MAP = $PlanMap }
    # 这些键一次都没设过，但历史上踩过（残留会让行为漂移），一并清掉。
    foreach ($k in @("UPLOAD_MAX_MB", "CB_FAILURE_THRESHOLD", "CB_OPEN_SECONDS")) {
        Remove-Item ("env:" + $k) -ErrorAction SilentlyContinue
    }

    $log = Join-Path $env:TEMP ("gw-allstages-" + $Name + ".log")
    # `-WindowStyle Hidden` 不是美观问题：DETACHED 的网关会绑定调用者的控制台，
    # 窗口一关它就收到 shutdown 信号退出（实测因此丢过 5 条断言）。
    $p = Start-Process -FilePath $exe -WorkingDirectory $repo `
        -RedirectStandardOutput $log -RedirectStandardError ($log + ".err") `
        -WindowStyle Hidden -PassThru
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if ($p.HasExited) {
            Write-Host ("  实例 " + $Name + " 启动失败，stderr 尾部：") -ForegroundColor Red
            Get-Content ($log + ".err") -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-Object -Last 5 | ForEach-Object { Write-Host ("    " + $_) -ForegroundColor DarkGray }
            return $null
        }
        # 就绪探测一律用 `curl.exe --noproxy '*'`，不用 `Invoke-WebRequest`：
        # PS 5.1 的 IWR 会走系统代理（本机常驻 Clash 一类），对 `127.0.0.1`
        # 也可能超时 —— 实测同一个 `/health`：IWR 10s 超时、curl 412ms。
        # 用 IWR 会让「实例其实起来了」被报成「20s 内未就绪」而整轮中止。
        $code = 0
        try {
            $code = [int](curl.exe -s -m 3 -o NUL -w '%{http_code}' --noproxy '*' ("http://127.0.0.1:" + $Port + "/health/live"))
        } catch { $code = 0 }
        if ($code -eq 200) {
            Write-Host ("  实例 " + $Name + " 就绪：http://127.0.0.1:" + $Port + "（日志 " + $log + "）") -ForegroundColor DarkGray
            return $p
        }
    }
    Write-Host ("  实例 " + $Name + " 20s 内未就绪（端口 $Port）") -ForegroundColor Red
    return $null
}

# ---- 等 login_ip 限流窗口翻过 --------------------------------------------------
#
# 判定方式是**发一次坏登录看还限不限**，而不是盲等：窗口的 bucket 由时间推导，
# 我们拿不到它的名字（也不该去猜 Redis 键名 —— 那会把验收绑死在内部实现上）。
# 非 429（正常情况是 401）= 窗口已经干净；429 = 还在窗口内。
function Wait-ForRateWindow {
    param([int]$Port, [int]$MaxSeconds = 75)
    $bogus = "gatewait_" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + "@example.com"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
        $code = 0
        $body = (@{ email = $bogus; password = "Wrong-Passw0rd!" } | ConvertTo-Json -Compress)
        try {
            # 同样走 curl.exe：IWR 的系统代理会让这里的判定跟着网络环境漂。
            $code = [int](curl.exe -s -m 10 -o NUL -w '%{http_code}' --noproxy '*' -X POST `
                    -H 'Content-Type: application/json' --data-binary $body `
                    ("http://127.0.0.1:" + $Port + "/api/v1/auth/login"))
        } catch { $code = 0 }
        if ($code -ne 429) {
            Write-Host ("  限流窗口已翻过（探测得到 " + $code + "，等了 " + [int]$sw.Elapsed.TotalSeconds + "s）") -ForegroundColor DarkGray
            return $true
        }
        Start-Sleep -Seconds 5
    }
    Write-Host ("  ⚠ 等了 " + $MaxSeconds + "s 仍是 429；M5 的限流断言可能因此退化") -ForegroundColor Yellow
    return $false
}

# ---- 等 AI 侧把上一轮的后台向量化消化完 ----------------------------------------
#
# 这类污染**没有共享状态、没有 429**，只有「机器变慢」，是最难认的一种：
#
#   M5 有一项 8MB 上传（实测**切出 16,969 块、被 MAX_DOC_CHUNKS 截断成 10,000 块**）。
#   AI 侧的向量化虽然已经在线程池里跑（`asyncio.to_thread`），但 torch 默认把线程数
#   设成逻辑核数 ⇒ 整机的核被吃满，同机的 MySQL / Redis / 网关一起变慢
#   （已新增 `TORCH_NUM_THREADS` 开关，见 ai-platform docs/09 §7.2）。
#   上传接口返回 ≠ 算完，后台还在啃。紧接着再跑一遍全套时，连 AI 的健康检查
#   都会被拖到几百毫秒，于是 M4 的两条**时序**断言就假红：
#
#       `S6：断连后已生成内容落库（≤1s）`    实测 5806 ms
#       `网关自身额外延迟（首帧中位差）≤ 30ms` 实测 48.8 ms
#
#   判据：**单跑 M4 绿、跑完 M5 紧接着再跑 M4 红 ⇒ 是残留负载，不是缺陷。**
#   （实测同一台机器：连跑两遍时 M1 41s→75s、M2 71s→186s、M5 85s→223s，
#    整体慢 2~3 倍，与产品行为无关。）
#
# 探测手段直接用**网关自己的 `/health`**：它已经把一次 AI 探测的往返耗时报成
# `checks.ai_platform.latency_ms`。
# ⚠ 不要用 `Invoke-WebRequest` 自己测：PS 5.1 的 IWR 会走系统代理
#   （本机常驻 Clash 一类），对 `127.0.0.1` 也要几百毫秒到超时，测不出真实差异 ——
#   实测同一个 `/health`：IWR 10s 超时，`curl.exe --noproxy '*'` 412ms。
function Wait-AIDrain {
    param([int]$Port, [int]$MaxHealthMs, [int]$NeedSamples = 3, [int]$MaxSeconds = 150)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $good = 0
    $lastMs = -1
    while ($sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
        $text = (curl.exe -s -m 5 --noproxy '*' ("http://127.0.0.1:" + $Port + "/health") | Out-String)
        $ms = -1
        $ok = $false
        try {
            $chk = ($text | ConvertFrom-Json).checks.ai_platform
            if ($chk) {
                if ($chk.PSObject.Properties.Name -contains 'latency_ms') { $ms = [int]$chk.latency_ms }
                $ok = [bool]$chk.ok
            }
        } catch { }
        if (-not $ok) {
            Write-Host "  ⚠ 网关 /health 读不到 ai_platform 检查项，跳过 AI 排空等待" -ForegroundColor Yellow
            return $true
        }
        $lastMs = $ms
        if ($ms -le $MaxHealthMs) { $good++ } else { $good = 0 }
        if ($good -ge $NeedSamples) {
            Write-Host ("  AI 已排空（健康探测 " + $ms + "ms ≤ " + $MaxHealthMs + "ms，连测 " + $NeedSamples +
                " 次，等了 " + [int]$sw.Elapsed.TotalSeconds + "s）") -ForegroundColor DarkGray
            return $true
        }
        Start-Sleep -Seconds 3
    }
    Write-Host ("  ⚠ 等了 " + $MaxSeconds + "s，AI 健康探测仍是 " + $lastMs + "ms（> " + $MaxHealthMs +
        "ms）：AI 侧后台还在向量化。") -ForegroundColor Yellow
    Write-Host "    M4 的两条时序断言（落库 ≤1s、网关额外延迟 ≤30ms）大概率会假红，别当产品缺陷。" -ForegroundColor Yellow
    return $false
}

# ---- 预检：AI 侧两个进程必须在 --------------------------------------------------
Write-Host ""
Write-Host "==== 预检 ====" -ForegroundColor Cyan
$aiOk = $false
try {
    $aiOk = ([int](curl.exe -s -m 8 -o NUL -w '%{http_code}' --noproxy '*' ($AIBaseUrl + "/api/v1/health")) -eq 200)
} catch { $aiOk = $false }
if (-not $aiOk) {
    Write-Host ("  AI HTTP 不可用：" + $AIBaseUrl + "/api/v1/health") -ForegroundColor Red
    Write-Host "  M3/M4/M5/M6 都会大面积失败。先起 AI 侧两个进程（见本文件头部注释）。" -ForegroundColor Red
    Write-Host "  ⚠ 如果 AI 进程在监听但所有请求都超时，先分清是哪种：" -ForegroundColor Yellow
    Write-Host "    · 事件循环被同步 CPU 占住（缺陷；解析/切分/哈希已改为 asyncio.to_thread，见 ai-platform docs/09 §7.1）" -ForegroundColor Yellow
    Write-Host "    · 还是向量化吃满核把整机拖慢（设计取舍；可设 TORCH_NUM_THREADS 收窄，见 §7.2）" -ForegroundColor Yellow
    exit 1
}
$aiPort = [int]($AIGrpcTarget -split ':')[-1]
$grpcOk = [bool](Get-NetTCPConnection -LocalPort $aiPort -State Listen -ErrorAction SilentlyContinue)
Write-Host ("  AI HTTP  OK（" + $AIBaseUrl + "）") -ForegroundColor DarkGray
if (-not $grpcOk) {
    Write-Host ("  AI gRPC 未监听：" + $AIGrpcTarget + " —— M3/M4/M5 的真实编排会失败") -ForegroundColor Yellow
} else {
    Write-Host ("  AI gRPC  OK（" + $AIGrpcTarget + "）") -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "==== 编译验收用的网关二进制 ====" -ForegroundColor Cyan
& go build -o $exe ./cmd/server
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
    Write-Host "  go build 失败" -ForegroundColor Red
    exit 1
}
Write-Host ("  OK " + $exe) -ForegroundColor DarkGray

Write-Host ""
Write-Host "==== 拉起两个专用实例 ====" -ForegroundColor Cyan
# 实例 A（M1–M4、M6）：**全部**限流键放宽到 1000。
# M1–M4、M6 里没有一条断言提到 429（可用 `Select-String 429` 自查），
# 放宽只会让「与限流无关的断言」不再被限流误伤，不会削弱任何验收点。
# 具体踩过的坑：
#   * `MSG_RATE_PER_MINUTE` 默认 **20**，而 M2 用同一个用户发了 **23** 条消息
#     （18 条显式 + G 段 5 条并发）→ 第 21 条起返回 429 →
#     报成 `I6 sending to a deleted conversation -> 404 :: expected=404 actual=429`。
#     消息桶是 **per user**（`RateLimitByUser(..., RateScopeMessage)`），
#     跟打哪个实例无关，所以只能靠放宽上限解决。
#   * `LOGIN_RATE_PER_MINUTE`/`LOGIN_ACCOUNT_RATE_PER_HOUR` 同理，且 login 桶
#     按**客户端 IP** 存在 Redis 里 ⇒ 与 M5 共享（M5 故意打满它）。
$stageRateEnv = @{
    LOGIN_RATE_PER_MINUTE        = "1000"
    LOGIN_ACCOUNT_RATE_PER_HOUR  = "1000"
    MSG_RATE_PER_MINUTE          = "1000"
    CONV_RATE_PER_MINUTE         = "1000"
    UPLOAD_RATE_PER_MINUTE       = "1000"
}
# 实例 A 的配额同样放宽，理由见 `Start-StageGateway` 里那段注释：
# 验收脚本用**固定 user_id**，而 `chat_requests` 是按自然日累计的，
# 反复跑全套会把它累到 `DefaultPlanLimits()` 的 100/日 之上。
# `concurrency` 也一起放大：M2 的 G 段是 5 并发真实模型调用，free 档正好是 5，
# 机器一忙就会把「排队等待」变成拒绝（那是环境噪声，不是产品行为）。
$stagePlanMap = '{"free":{"chat_requests":1000000,"llm_tokens":2000000000,' +
'"kb_count":100000,"documents_count":1000000,"storage_bytes":1099511627776,"concurrency":200}}'
$stageGw = Start-StageGateway -Port $StagePort -Mp $StageMetricsPort `
    -RateEnv $stageRateEnv -PlanMap $stagePlanMap -Name "A"
if ($stageGw) { [void]$script:Started.Add($stageGw) }
# 实例 B（只有 M5）：**空档案 + 空套餐** = 全部走生产默认。
# 这里必须刻意「什么都不放宽」：M5 是全套里**唯一**断言限流与配额拦截的阶段，
# 它的 429 断言只有在默认上限下才成立（放宽了会变成永远不触发 → 假绿）。
$limitGw = Start-StageGateway -Port $LimitPort -Mp $LimitMetricsPort `
    -RateEnv @{} -PlanMap "" -Name "B"
if ($limitGw) { [void]$script:Started.Add($limitGw) }
if (-not $stageGw -or -not $limitGw) {
    foreach ($p in $script:Started) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}

# 开跑前等 AI 侧把上一轮（尤其是 M5 那 8MB 上传）的后台向量化算完。
# 放在实例起来之后：这个信号来自网关自己的 `/health`。
Write-Host ""
Write-Host "==== 等 AI 侧排空 ====" -ForegroundColor Cyan
[void](Wait-AIDrain -Port $StagePort -MaxHealthMs $AIDrainMaxMs)

# ---- 阶段表 ------------------------------------------------------------------
#
# `Min` 是**下限**而不是等号：断言只会增加，写死等号会让每加一条断言都要改这里。
# `SkipMin` 是传了 `-SkipChat` 时的下限 —— `-SkipChat` 会跳过所有真实模型调用，
# 相关的断言自然就少了，拿完整模式的下限去比会假红。
#   M3：完整 29 → `-SkipChat` 16（跳过的 13 条都是「编排成功/回答非空」类）
#   M4：完整 50 → `-SkipChat` 30（跳过的 20 条都是「流式帧/增量对比」类）
# M1/M2/M5/M6 的 `-SkipChat` 没有意义（它们要么不跑模型，要么参数名不同）。
$stageBase = "http://127.0.0.1:$StagePort"
$limitBase = "http://127.0.0.1:$LimitPort"
$stages = @(
    @{ Name = 'M1'; Script = 'curl_stage1.ps1'; Min = 99; Base = $stageBase }
    @{ Name = 'M2'; Script = 'curl_stage2.ps1'; Min = 198; Base = $stageBase }
    @{ Name = 'M3'; Script = 'curl_stage3.ps1'; Min = 29; SkipMin = 16; Base = $stageBase }
    @{ Name = 'M4'; Script = 'curl_stage4.ps1'; Min = 50; SkipMin = 30; Base = $stageBase }
    # M5 跑在**默认限流**的实例上：它是唯一断言限流的阶段（其余阶段 grep 429 命中 0）。
    @{ Name = 'M5'; Script = 'curl_stage5.ps1'; Min = 36; Base = $limitBase; NeedRateWindow = $true }
    @{ Name = 'M6'; Script = 'curl_stage6.ps1'; Min = 56; Base = $stageBase }
)

$rows = New-Object System.Collections.ArrayList
$anyFail = $false

Write-Host ""
Write-Host ("==== 全阶段验收（顺序 M1→M6，共 " + $stages.Count + " 段）====") -ForegroundColor Cyan

foreach ($s in $stages) {
    if ($s.NeedRateWindow) {
        Write-Host ""
        Write-Host ("---- " + $s.Name + " 前置：等 login_ip 限流窗口翻过（共享 Redis，跨实例）----") -ForegroundColor Yellow
        [void](Wait-ForRateWindow -Port $LimitPort)
    }

    $path = Join-Path $PSScriptRoot $s.Script
    $outFile = Join-Path $env:TEMP ("stage" + $s.Name.Substring(1) + "-final.txt")
    # ⚠️ 变量名不能叫 `$args`：那是 PowerShell 的自动变量（存未绑定参数），
    # 覆盖它不报错但会误导读者。
    $pwArgs = @('-ExecutionPolicy', 'Bypass', '-File', $path, '-BaseUrl', $s.Base)
    # 有效下限：`-SkipChat` 会让 M3/M4 少跑一批断言，下限要跟着降。
    $min = $s.Min
    if ($SkipChat -and ($s.Name -eq 'M3' -or $s.Name -eq 'M4')) { $pwArgs += '-SkipChat' }
    if ($SkipChat -and $s.ContainsKey('SkipMin')) { $min = $s.SkipMin }

    Write-Host ""
    Write-Host ("---- " + $s.Name + " : " + $s.Script + " -> " + $s.Base + " ----") -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # 子进程是独立的 powershell.exe，它的 `Write-Host` 走继承来的 stdout 句柄，
    # 所以**能被管道落盘**（不需要 `*>&1`）。
    # 不加 `2>&1 |`：混流会让报错行被当成断言行。
    & powershell @pwArgs | Out-File -Encoding UTF8 $outFile
    $sw.Stop()

    # 判定一律以**落盘文件**为准：终端里的中文可能只是显示假象。
    $text = Get-Content $outFile -Encoding UTF8 -Raw
    # M1–M4 用 `[PASS]`，M5 起用 `PASS `（中文脚本），两种都要认。
    $pass = ([regex]::Matches($text, '(?m)^\s*(\[PASS\]|PASS)\s')).Count
    $fail = ([regex]::Matches($text, '(?m)^\s*(\[FAIL\]|FAIL)\s')).Count
    $skip = ([regex]::Matches($text, '(?m)^\s*(\[SKIP\]|SKIP)\s')).Count

    $ok = ($fail -eq 0) -and ($pass -ge $min)
    if (-not $ok) { $anyFail = $true }
    [void]$rows.Add([pscustomobject]@{
            Stage = $s.Name; Pass = $pass; Fail = $fail; Skip = $skip
            Min = $min; Seconds = [int]$sw.Elapsed.TotalSeconds; Ok = $ok
        })
    $color = 'Green'; if (-not $ok) { $color = 'Red' }
    $verdict = 'OK'; if (-not $ok) { $verdict = 'NG' }
    Write-Host ("     PASS=" + $pass + " FAIL=" + $fail + " SKIP=" + $skip +
        " 期望>=" + $min + " 耗时=" + [int]$sw.Elapsed.TotalSeconds + "s => " + $verdict) -ForegroundColor $color
    if (-not $ok) {
        Write-Host ("     详见 " + $outFile) -ForegroundColor Red
        Select-String -Path $outFile -Pattern '(\[FAIL\]|FAIL )' -Encoding UTF8 |
            Select-Object -First 6 | ForEach-Object { Write-Host ("       " + $_.Line.Trim()) -ForegroundColor Red }
    }
}

if (-not $KeepGateway) {
    Write-Host ""
    Write-Host "==== 清理专用实例 ====" -ForegroundColor Cyan
    foreach ($p in $script:Started) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Write-Host ("  stopped pid=" + $p.Id) -ForegroundColor DarkGray
        }
    }
} else {
    Write-Host ""
    Write-Host "==== -KeepGateway：两个专用实例保留运行 ====" -ForegroundColor Yellow
    foreach ($p in $script:Started) { Write-Host ("  pid=" + $p.Id) -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "==== 汇总 ====" -ForegroundColor Cyan
$rows | Format-Table -AutoSize
$totalPass = ($rows | Measure-Object -Property Pass -Sum).Sum
$totalFail = ($rows | Measure-Object -Property Fail -Sum).Sum
Write-Host ("总断言 " + $totalPass + " 条；失败 " + $totalFail + " 条") -ForegroundColor Cyan

if ($anyFail) {
    Write-Host "=== 全阶段验收：存在失败 ===" -ForegroundColor Red
    exit 1
}
Write-Host "=== 全阶段验收：全部通过 ===" -ForegroundColor Green
exit 0
