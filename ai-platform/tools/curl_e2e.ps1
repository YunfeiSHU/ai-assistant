# End-to-end curl sweep over every public endpoint of ai-platform.
#
#   powershell -File tools/curl_e2e.ps1
#   powershell -File tools/curl_e2e.ps1 -Base http://127.0.0.1:8000/api/v1
#
# Why a script and not a pile of curl commands:
#   * SSE endpoints need `curl -N` and a bounded `--max-time`, otherwise the
#     command hangs forever on a server that keeps the connection open.
#   * The document pipeline is asynchronous: the assertions only make sense
#     after the ingestion task reached a terminal status, so there is a poll loop.
#   * "Did everything pass" must be machine-decidable. A handful of non-2xx
#     responses are the CORRECT outcome (409 on cancel/retry of a finished task,
#     404 for a summary that has not been built yet, 404 for an unknown MCP
#     server). Those are listed in $script:expected below; anything else
#     non-2xx is reported as UNEXPECTED and makes the script exit non-zero.
#
# ASCII only on purpose: this file is written without a BOM, and PowerShell 5.1
# decodes BOM-less files as GBK, which corrupts non-ASCII quotes/comments.

param(
    [string]$Base = "http://127.0.0.1:8000/api/v1",
    [string]$LogPath = ""
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$base = $Base.TrimEnd("/")
$work = Join-Path $env:TEMP "ai_curl"
New-Item -ItemType Directory -Force -Path $work | Out-Null
if (-not $LogPath) { $LogPath = Join-Path $work "curl_log.txt" }

$script:log = New-Object System.Collections.ArrayList
$script:passed = 0
$script:total = 0
$script:unexpected = New-Object System.Collections.ArrayList

# Non-2xx that are the documented, correct answer.
$script:expected = @{
    "cancel task"          = "409 TASK_NOT_CANCELABLE (the task already finished)"
    "retry task"           = "409 TASK_NOT_RETRYABLE (the task succeeded)"
    "conversation summary" = "404 SUMMARY_NOT_FOUND (no summary built yet)"
    "mcp reload"           = "404 MCP_SERVER_NOT_FOUND"
    "mcp tools"            = "404 MCP_SERVER_NOT_FOUND"
    "get kb after delete"  = "404 KB_NOT_FOUND (soft-deleted knowledge base must disappear)"
}

function Show([string]$text) { [void]$script:log.Add($text) }

function Record {
    param([string]$name, [string]$code, [string]$apiPath = "", [string]$method = "")
    $script:total = $script:total + 1
    if ("$code" -match "^2") {
        $script:passed = $script:passed + 1
        return
    }
    if ($script:expected.ContainsKey($name)) {
        $script:log.Add("    (expected non-2xx: $($script:expected[$name]))")
        return
    }
    [void]$script:unexpected.Add("$name -> $method $apiPath [HTTP $code]")
}

function Req {
    param(
        [string]$name,
        [string]$method,
        [string]$path,
        [string]$body = "",
        [string[]]$extra = @(),
        [int]$timeout = 30
    )
    $out = Join-Path $work "resp.json"
    # `--noproxy *`：本机常驻 Clash 一类代理时会设 `http_proxy` 环境变量，而 curl 是认
    # 环境变量的（不像 Invoke-WebRequest 走 WinINET）。本脚本全部请求都打 127.0.0.1，
    # 显式绕开代理才不会被「代理是否在跑」影响结论（docs/09-§2.1 是同一件事的另一面）。
    $av = @("-s", "--noproxy", "*", "-o", $out, "-w", "%{http_code}", "--max-time", "$timeout", "-X", $method, "$base$path", "-H", "Accept: application/json")
    if ($body) {
        $bodyFile = Join-Path $work "body.json"
        [IO.File]::WriteAllText($bodyFile, $body, [Text.UTF8Encoding]::new($false))
        $av += @("-H", "Content-Type: application/json", "--data-binary", "@$bodyFile")
    }
    if ($extra) { $av += $extra }
    $code = & curl.exe @av 2>&1
    $full = ""
    if (Test-Path $out) { $full = [IO.File]::ReadAllText($out, [Text.UTF8Encoding]::new($false)) }
    Record $name "$code" $path $method
    Show ""
    Show "### $name  ->  $method $path  [HTTP $code]"
    if ($body) { Show "REQ  $body" }
    # Truncate a COPY for the log only. Truncating $full in place would hand the
    # caller a cut-off JSON string and every ConvertFrom-Json below would fail
    # (that bug once made "rerank_used=" read as empty even though the call
    # succeeded -- a false negative in the harness, not in the service).
    $shown = $full
    if ($shown.Length -gt 1200) { $shown = $shown.Substring(0, 1200) + " ...(truncated)" }
    Show "RESP $shown"
    return $full
}

function AsJson([string]$text) {
    try { return $text | ConvertFrom-Json } catch { return $null }
}

# Poll a task until it reaches a terminal status (ingestion runs in the background).
function Poll {
    param([string]$id, [int]$maxSeconds = 600)
    $deadline = (Get-Date).AddSeconds($maxSeconds)
    $last = ""
    while ((Get-Date) -lt $deadline) {
        $out = Join-Path $work "poll.json"
        & curl.exe -s --noproxy * -o $out "$base/tasks/$id" | Out-Null
        $task = AsJson ([IO.File]::ReadAllText($out, [Text.UTF8Encoding]::new($false)))
        if ($task) {
            $last = "$($task.status)"
            if (@("SUCCEEDED", "FAILED", "CANCELED") -contains $last) {
                Show ""
                Show "### poll task $id  ->  $last  (stage=$($task.stage) progress=$($task.progress))"
                if ($last -ne "SUCCEEDED") { [void]$script:unexpected.Add("task $id ended as ${last} / $($task.error)") }
                return $task
            }
        }
        Start-Sleep -Seconds 3
    }
    Show ""
    Show "### poll task $id timed out (last=$last)"
    [void]$script:unexpected.Add("task $id timed out")
    return $null
}

# SSE endpoints: verbatim capture of the stream (no JSON parsing).
function ReqRaw {
    param(
        [string]$name,
        [string]$method,
        [string]$path,
        [string]$body = "",
        [int]$timeout = 30
    )
    $out = Join-Path $work "sse.txt"
    $av = @("-s", "-N", "--noproxy", "*", "--max-time", "$timeout", "-o", $out, "-w", "%{http_code}", "-X", $method, "$base$path", "-H", "Accept: text/event-stream")
    if ($body) {
        $bodyFile = Join-Path $work "sse-body.json"
        [IO.File]::WriteAllText($bodyFile, $body, [Text.UTF8Encoding]::new($false))
        $av += @("-H", "Content-Type: application/json", "--data-binary", "@$bodyFile")
    }
    $code = & curl.exe @av 2>&1
    $text = ""
    if (Test-Path $out) { $text = [IO.File]::ReadAllText($out, [Text.UTF8Encoding]::new($false)) }
    Record $name "$code" $path $method
    Show ""
    Show "### $name  ->  $method $path  [HTTP $code] (SSE)"
    if ($body) { Show "REQ  $body" }
    $rewritten = @()
    foreach ($line in ($text -split "`r?`n")) {
        if ($line.Length -gt 220) { $line = $line.Substring(0, 220) + " ...(truncated)" }
        $rewritten += $line
    }
    Show ("RESP " + ($rewritten -join "`r`n"))
    return $text
}

# --------------------------------------------------------------------------
Show "================ HEALTH ================"
Req "health" "GET" "/health" | Out-Null
Req "health live" "GET" "/health/live" | Out-Null
Req "health ready" "GET" "/health/ready" | Out-Null

Show ""
Show "================ META ================"
Req "models" "GET" "/models" | Out-Null
$toolsText = Req "tools list" "GET" "/tools"
$tools = AsJson $toolsText
$toolName = "current_time"
$toolArgs = "{}"
foreach ($item in @($tools.items)) {
    if ($item.name -eq "calculator") {
        $toolName = "calculator"
        $toolArgs = ($item.example_arguments | ConvertTo-Json -Compress -Depth 5)
    }
}
if ($toolArgs -eq "" -or $toolArgs -eq "null") { $toolArgs = "{}" }
Req "tool invoke ($toolName)" "POST" "/tools/$toolName/invoke" "{`"arguments`":$toolArgs}" | Out-Null

Show ""
Show "================ KNOWLEDGE BASE ================"
$kbName = "e2e-kb-" + (Get-Date -Format "HHmmss")
$kbText = Req "create kb" "POST" "/knowledge-bases" "{`"name`":`"$kbName`",`"description`":`"e2e Probe`",`"chunk_size`":256,`"chunk_overlap`":32}"
$kb = AsJson $kbText
$kbId = "$($kb.id)"
Req "list kb" "GET" "/knowledge-bases" | Out-Null
Req "get kb" "GET" "/knowledge-bases/$kbId" | Out-Null
Req "patch kb" "PATCH" "/knowledge-bases/$kbId" "{`"description`":`"patched by e2e`"}" | Out-Null

Show ""
Show "================ DOCUMENT ================"
$srcFile = Join-Path $work "e2e-doc.txt"
$lines = @()
for ($i = 1; $i -le 40; $i++) {
    $lines += "Section ${i}: the ai-platform e2e probe paragraph number $i, used to verify ingestion, chunking and retrieval."
}
[IO.File]::WriteAllLines($srcFile, $lines, [Text.UTF8Encoding]::new($false))
$upText = Req "upload document (multipart)" "POST" "/knowledge-bases/$kbId/documents" "" @("-F", "file=@$srcFile;type=text/plain", "-F", "doc_name=e2e-doc.txt")
$up = AsJson $upText
$docId = "$($up.doc_id)"
$taskId = "$($up.task_id)"
$done = Poll $taskId
Req "list documents" "GET" "/knowledge-bases/$kbId/documents" | Out-Null
Req "get document" "GET" "/documents/$docId" | Out-Null
Req "list chunks" "GET" "/documents/$docId/chunks" | Out-Null
Req "search in kb" "POST" "/knowledge-bases/$kbId/search" "{`"query`":`"section 7 probe`",`"top_k`":3,`"with_rerank`":false}"
$rrText = Req "search in kb (rerank)" "POST" "/knowledge-bases/$kbId/search" "{`"query`":`"section 7 probe`",`"top_k`":3,`"with_rerank`":true}" -timeout 600
$rr = AsJson $rrText
# Parenthesise the concatenation: in argument mode PowerShell would otherwise
# pass "scores=" and the joined list as SEPARATE arguments to Show (a silent,
# non-terminating error) and the score list would come out empty.
$rrScores = (@($rr.items | ForEach-Object { $_.rerank_score }) -join ",")
Show ("    rerank_used=" + $rr.rerank_used + " returned=" + $rr.returned + " rerank_scores=" + $rrScores)
if ($rr.rerank_used -ne $true) { [void]$script:unexpected.Add("rerank not applied: rerank_used=$($rr.rerank_used)") }

Show ""
Show "================ TASKS ================"
Req "list tasks" "GET" "/tasks" | Out-Null
Req "get task" "GET" "/tasks/$taskId" | Out-Null
ReqRaw "task events (SSE)" "GET" "/tasks/$taskId/events" | Out-Null
Req "cancel task" "POST" "/tasks/$taskId/cancel" "" | Out-Null
Req "retry task" "POST" "/tasks/$taskId/retry" "" | Out-Null

Show ""
Show "================ CHAT ================"
$chatText = Req "chat" "POST" "/chat" "{`"query`":`"Say hello in one short sentence.`",`"use_rag`":false,`"use_memory`":true,`"use_tools`":false}" -timeout 120
$chat = AsJson $chatText
$convId = "$($chat.conversation_id)"
ReqRaw "chat stream" "POST" "/chat/stream" "{`"query`":`"Say hi in one short sentence.`",`"use_rag`":false,`"use_memory`":true,`"use_tools`":false}" -timeout 120 | Out-Null

Show ""
Show "================ AGENT ================"
$agentText = Req "agent run" "POST" "/agent/run" "{`"query`":`"What time is it now? Use the current_time tool.`",`"use_tools`":true,`"use_rag`":false,`"use_memory`":false,`"max_steps`":3}" -timeout 180
$agent = AsJson $agentText
if ($convId -eq "" -or $convId -eq "null") { $convId = "$($agent.conversation_id)" }
ReqRaw "agent run stream" "POST" "/agent/run/stream" "{`"query`":`"What time is it now?`",`"use_tools`":true,`"use_rag`":false,`"use_memory`":false,`"max_steps`":3}" -timeout 180 | Out-Null

Show ""
Show "================ CONVERSATION MEMORY ================"
Req "conversation context" "GET" "/conversations/$convId/context" | Out-Null
Req "conversation summary" "GET" "/conversations/$convId/summary" | Out-Null
Req "rebuild summary" "POST" "/conversations/$convId/summary/rebuild" "" | Out-Null
Req "delete context" "DELETE" "/conversations/$convId/context" | Out-Null

Show ""
Show "================ LONG TERM MEMORY ================"
$memText = Req "create memory" "POST" "/memories" "{`"content`":`"The user prefers answers in Chinese.`",`"kind`":`"preference`",`"confidence`":0.9}"
$mem = AsJson $memText
$memId = "$($mem.id)"
Req "create duplicate memory" "POST" "/memories" "{`"content`":`"The user prefers answers in Chinese.`",`"kind`":`"preference`"}" | Out-Null
Req "list memories" "GET" "/memories" | Out-Null
Req "list memories (all=true)" "GET" "/memories?all=true" | Out-Null
Req "get memory" "GET" "/memories/$memId" | Out-Null
Req "patch memory" "PATCH" "/memories/$memId" "{`"kind`":`"fact`"}" | Out-Null
Req "get memory settings" "GET" "/memory-settings" | Out-Null
Req "put memory settings" "PUT" "/memory-settings" "{`"memory_enabled`":true,`"memory_top_n`":3}" | Out-Null
Req "delete memory" "DELETE" "/memories/$memId" | Out-Null
Req "delete all memories" "DELETE" "/memories?all=true" | Out-Null

Show ""
Show "================ MCP ================"
Req "mcp servers" "GET" "/mcp/servers" | Out-Null
Req "mcp reload" "POST" "/mcp/servers/not-exists/reload" "{`"force`":false}" | Out-Null
Req "mcp tools" "GET" "/mcp/servers/not-exists/tools" | Out-Null

Show ""
Show "================ CLEANUP ================"
$delText = Req "delete document" "DELETE" "/documents/$docId"
$del = AsJson $delText
if ($del -and "$($del.task_id)" -ne "" -and "$($del.task_id)" -ne "null") {
    Poll "$($del.task_id)" | Out-Null
}
Req "delete kb" "DELETE" "/knowledge-bases/$kbId" | Out-Null
Req "get kb after delete" "GET" "/knowledge-bases/$kbId" | Out-Null

Show ""
Show "================ SUMMARY ================"
Show "requests: $script:total, 2xx: $script:passed, unexpected failures: $($script:unexpected.Count)"
foreach ($item in $script:unexpected) { Show "UNEXPECTED: $item" }
Show "kb=$kbId doc=$docId task=$taskId conv=$convId mem=$memId"

$script:log -join "`r`n" | Out-File -Encoding UTF8 $LogPath
Write-Output "LOG=$LogPath"
Write-Output "REQUESTS=$script:total PASSED=$script:passed UNEXPECTED=$($script:unexpected.Count)"
if ($script:unexpected.Count -gt 0) { exit 1 }
exit 0
