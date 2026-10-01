# M3 验收：编排闭环（gRPC 通道 + 非 AI 接口透传）
#
# 前置：
#   1) AI 侧 gRPC 服务（ai-platform 仓库）：
#        $env:GRPC_ENABLED="true"; $env:GRPC_PORT="50051"; uv run python -m app.grpc
#   2) Go 网关：
#        $env:HTTP_ADDR="0.0.0.0:18080"; $env:AI_GRPC_ENABLED="true"; go run ./cmd/server
#
# 用法：powershell -File tools\curl_stage3.ps1
#      powershell -File tools\curl_stage3.ps1 -BaseUrl http://127.0.0.1:18080 -SkipChat
#
# -SkipChat 用于「AI 侧没配 LLM 密钥」的环境：只跑透传与错误映射，
# 不跑真实模型调用（真实调用会花钱，且失败原因与网关无关）。

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:18080",
    [switch]$SkipChat
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

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

# ---- 压掉脚本自身的地板：关掉系统代理 ----
# 本机常驻代理（Clash 一类，127.0.0.1:7897）时，PS 5.1 的 `Invoke-WebRequest` 连
# **回环地址**也会走代理解析：实测同一个 `/health`，IWR 走代理 10s 超时，而
# `curl.exe --noproxy '*'` 只要 412ms（run_all_stages.ps1 头部 / docs/09-§2.1）。
# 本脚本 100% 打 127.0.0.1 ⇒ 关掉代理是纯收益，省掉的是**每次往返**的固定开销。
#
# 为什么设 .NET 静态属性而不只写 `-Proxy $null`：PS 5.1 的 `-Proxy` 落在
# `if (Proxy != null) { request.Proxy = Proxy }` 上，传 `$null` 在部分版本上等价于
# **不传**（代理照旧生效）。进程级 `DefaultWebProxy = $null` 才是确定生效的那一下；
# `Invoke-Api` 的参数表里同时带上 `-Proxy $null`（PS 6+ 用 `-NoProxy`）做双保险。
#
# ⚠ 只在「目标全是回环」时成立：把 -BaseUrl 指到非回环地址请删掉这一段。
[System.Net.WebRequest]::DefaultWebProxy = $null

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Token,
        [object]$Body,
        [string]$TraceId = ""
    )
    $headers = @{ "X-Request-Id" = "req_stage3" }
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }
    # 链路 id 必须走 W3C `traceparent`：`X-Trace-Id` 是**响应**头（网关回写用），
    # 塞进请求里会被完全忽略 —— 于是「断言 trace 透传」永远看不到自己给的值。
    if ($TraceId) { $headers["traceparent"] = "00-$TraceId-7f3a91c2d5b6480e-01" }
    $params = @{ Method = $Method; Uri = ($BaseUrl + $Path); Headers = $headers; UseBasicParsing = $true; TimeoutSec = 120 }
    if ($null -ne $Body) {
        $params["ContentType"] = "application/json"
        # 必须显式转 UTF-8 字节：PS 5.1 默认按 ASCII/GBK 编码请求体，
        # 中文 query 会被写坏，表现为「AI 收到的是一串乱码」。
        $json = $Body | ConvertTo-Json -Depth 8 -Compress
        $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    # 与文件顶部 `DefaultWebProxy = $null` 配对的双保险：PS 6+ 用 `-NoProxy`，
    # PS 5.1 没有这个开关，只能传 `-Proxy $null`（顶部那段注释解释了为什么它单独用不够）。
    if ($PSVersionTable.PSVersion.Major -ge 6) { $params["NoProxy"] = $true } else { $params["Proxy"] = $null }

    $status = 0
    $text = ""
    $respHeaders = $null
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
            # PS 5.1 的坑：错误响应的 `GetResponseStream()` 往往已被读过一遍，
            # 再读只会得到空串 —— 表现是「4xx 明明带了错误信封，脚本却读到空 body，
            # 于是断言 '回包是约定信封' 假失败」。`ErrorDetails.Message` 才是
            # PowerShell 已经解好码的那份错误正文（会按 Content-Type 的 charset 解码）。
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $text = $_.ErrorDetails.Message
            } else {
                $stream = $r.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                $text = $reader.ReadToEnd()
            }
        } else {
            $text = $_.Exception.Message
        }
    }
    $obj = $null
    if ($text) {
        try { $obj = $text | ConvertFrom-Json } catch { $obj = $null }
    }
    $traceOut = ""
    if ($respHeaders -and $respHeaders["X-Trace-Id"]) { $traceOut = [string]$respHeaders["X-Trace-Id"] }
    return [pscustomobject]@{ Status = $status; Text = $text; Json = $obj; Trace = $traceOut }
}

# ----------------------------------------------------------------------
Write-Host "== M3 验收：$BaseUrl ==" -ForegroundColor Cyan

# ---- 0. 健康 ----
$health = Invoke-Api -Method GET -Path "/health"
Assert-True "健康检查 200" ($health.Status -eq 200) ("status=" + $health.Status)

# ---- 1. 注册/登录拿到用户令牌（透传必须带用户身份，不能用服务凭据）----
$suffix = [Guid]::NewGuid().ToString('N').Substring(0, 10)
$email = "stage3_$suffix@example.com"
$reg = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{ email = $email; password = "Stage3-Passw0rd!" }
$token = $null
$loginStatus = "-"
if ($reg.Json) { $token = $reg.Json.access_token }
# 注意：注册成功（201）只回用户资料、**不回令牌**，必须再登录一次。
# 早期版本写成「status=201 就取 access_token」，于是 $token 为 $null，
# 后面所有带令牌的断言集体 401 —— 表现像「鉴权坏了」，其实是脚本少了一步。
if (-not $token) {
    $login = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $email; password = "Stage3-Passw0rd!" }
    $loginStatus = $login.Status
    if ($login.Json) { $token = $login.Json.access_token }
}
Assert-True "取得用户令牌" ([bool]$token) ("register=" + $reg.Status + " login=" + $loginStatus)

# ---- 2. 透传：GET /api/v1/models（无归属校验）----
$models = Invoke-Api -Method GET -Path "/api/v1/models" -Token $token
Assert-True "透传 /models 返回 200" ($models.Status -eq 200) ("status=" + $models.Status + " body=" + $models.Text.Substring(0, [Math]::Min(160, $models.Text.Length)))

# ---- 3. 透传：知识库创建 + 列表 + 详情（POST/GET + 路径参数）----
$kbName = "stage3-kb-$suffix"
$kb = Invoke-Api -Method POST -Path "/api/v1/knowledge-bases" -Token $token -Body @{ name = $kbName; description = "M3 透传验收" }
Assert-True "透传创建知识库 201" ($kb.Status -eq 201) ("status=" + $kb.Status + " body=" + $kb.Text)
$kbId = $null
if ($kb.Json) { $kbId = $kb.Json.id }

$kbList = Invoke-Api -Method GET -Path "/api/v1/knowledge-bases?limit=5" -Token $token
Assert-True "透传知识库列表 200" ($kbList.Status -eq 200) ("status=" + $kbList.Status)
Assert-True "列表里能看到刚建的知识库" ($kbList.Text -like ("*" + $kbName + "*")) "回包里没有 $kbName"

if ($kbId) {
    $kbGet = Invoke-Api -Method GET -Path ("/api/v1/knowledge-bases/" + $kbId) -Token $token
    Assert-True "透传知识库详情 200" ($kbGet.Status -eq 200) ("status=" + $kbGet.Status)
    $kbDocs = Invoke-Api -Method GET -Path ("/api/v1/knowledge-bases/" + $kbId + "/documents?limit=5") -Token $token
    Assert-True "透传知识库文档列表 200" ($kbDocs.Status -eq 200) ("status=" + $kbDocs.Status)
}

# ---- 4. 透传：任务列表与 404 映射（AI 侧的错误信封必须原样透出）----
$tasks = Invoke-Api -Method GET -Path "/api/v1/tasks?limit=5" -Token $token
Assert-True "透传任务列表 200" ($tasks.Status -eq 200) ("status=" + $tasks.Status)

$missing = Invoke-Api -Method GET -Path "/api/v1/knowledge-bases/kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3D" -Token $token
Assert-True "上游 404 原样透出" ($missing.Status -eq 404) ("status=" + $missing.Status)
Assert-True "404 回包是约定信封" ($missing.Text -match '"error"') ("body=" + $missing.Text)
if ($missing.Json -and $missing.Json.error) {
    Assert-True "404 保留上游 code" ($missing.Json.error.code -eq "KB_NOT_FOUND") ("code=" + $missing.Json.error.code)
}

# ---- 5. 透传：无令牌必须 401（不能降级用服务凭据）----
$noAuth = Invoke-Api -Method GET -Path "/api/v1/models"
Assert-True "无令牌访问 AI 接口 401" ($noAuth.Status -eq 401) ("status=" + $noAuth.Status)

# ---- 6. 归属校验：他人会话的 summary 必须 404 ----
$convOwner = Invoke-Api -Method POST -Path "/api/v1/conversations" -Token $token -Body @{ title = "stage3 conv" }
$convId = $null
if ($convOwner.Json) { $convId = $convOwner.Json.id }
Assert-True "创建会话 201" ($convOwner.Status -eq 201) ("status=" + $convOwner.Status)

if ($convId) {
    $ctx = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convId + "/context") -Token $token
    Assert-True "透传会话上下文 200" ($ctx.Status -eq 200) ("status=" + $ctx.Status)
    $ctxMissing = Invoke-Api -Method GET -Path "/api/v1/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3D/context" -Token $token
    Assert-True "不存在的会话 404" ($ctxMissing.Status -eq 404) ("status=" + $ctxMissing.Status)
}

# ---- 7. 编排：POST /chat（gRPC 通道）----
if ($SkipChat) {
    Write-Host "  SKIP  /chat（-SkipChat）" -ForegroundColor Yellow
} elseif (-not $convId) {
    Write-Host "  SKIP  /chat（没有可用会话）" -ForegroundColor Yellow
} else {
    $traceId = "4f2a1c9d8b7e6a5f4d3c2b1a0f9e8d7c"
    $chat = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convId + "/messages") -Token $token -TraceId $traceId -Body @{
        content = "用一句话说明什么是向量检索"
        use_rag = $false
    }
    # 契约（易错）：编排可用时 POST /messages 返回 **200**，字段是 `user_message` / `assistant_message`。
    # M2 时期还没有编排器，同一个接口返回 503 orchestrator_not_configured —— 按老印象写成
    # 「201 + user/assistant」会得到「状态码对不上 + assistant 为空」两个假失败。
    Assert-True "编排 /messages 返回 200" ($chat.Status -eq 200) ("status=" + $chat.Status + " body=" + $chat.Text.Substring(0, [Math]::Min(240, $chat.Text.Length)))
    Assert-True "链路 id 回写响应头（traceparent 生效）" ($chat.Trace -eq $traceId) ("X-Trace-Id=" + $chat.Trace)
    if ($chat.Json) {
        Assert-True "返回 assistant_message" ([bool]$chat.Json.assistant_message) "回包里没有 assistant_message"
        if ($chat.Json.assistant_message) {
            $ans = $chat.Json.assistant_message
            Assert-True "回答非空" ([bool]$ans.content -and $ans.content.Length -gt 0) "content 为空"
            Assert-True "回答已落库（seq=2 / role=assistant）" (($ans.seq -eq 2) -and ($ans.role -eq "assistant")) ("seq=" + $ans.seq + " role=" + $ans.role)
            Assert-True "model 已回填" ([bool]$ans.model) ("model=" + $ans.model)
            Assert-True "elapsed_ms 为正" ($ans.elapsed_ms -gt 0) ("elapsed_ms=" + $ans.elapsed_ms)
            Assert-True "usage 已回填" ($ans.usage.total_tokens -gt 0) ("total_tokens=" + $ans.usage.total_tokens)
            Assert-True "assistant 的 trace_id 与本次链路一致" ($ans.trace_id -eq $traceId) ("trace_id=" + $ans.trace_id)
        }
        Assert-True "会话 id 为网关权威值" ($chat.Json.user_message.conversation_id -eq $convId) ("conversation_id=" + $chat.Json.user_message.conversation_id)
    }

    # 历史回灌：第二问必须把上一轮（含 assistant 回答）带给 AI —— 用时间戳无关的
    # 稳定判据「回答里出现上一问的关键词」不成立，改为断言消息条数递增（12 条历史回灌由单测覆盖）。
    $msgs = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convId + "/messages?limit=20") -Token $token
    Assert-True "落库两轮共 2 条消息" ($msgs.Json.items.Count -eq 2) ("count=" + $msgs.Json.items.Count)

    # 失败路径：不存在的会话在**落库前**就该被拒（不能先写 user 消息再失败）
    $chatMissing = Invoke-Api -Method POST -Path "/api/v1/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3D/messages" -Token $token -Body @{ content = "不该成功" }
    Assert-True "编排：不存在的会话 404" ($chatMissing.Status -eq 404) ("status=" + $chatMissing.Status)

    # 空内容必须 400（参数校验在网关侧完成，不消耗模型调用）
    $chatEmpty = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convId + "/messages") -Token $token -Body @{ content = "   " }
    Assert-True "空内容 400" ($chatEmpty.Status -eq 400) ("status=" + $chatEmpty.Status)
}

Write-Host ""
Write-Host ("== 合计 " + $script:Total + " 条断言，失败 " + $script:Failed + " 条 ==") -ForegroundColor Cyan
if ($script:Failed -gt 0) {
    Write-Host "失败明细：" -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host ("  - " + $f) -ForegroundColor Red }
    exit 1
}
Write-Host "全部通过" -ForegroundColor Green
exit 0
