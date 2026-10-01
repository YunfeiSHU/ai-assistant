# =============================================================================
# M2 stage acceptance: conversations / messages / idempotency
#
# ASCII only -- PowerShell 5.1 parses BOM-less files as GBK, so any non-ASCII
# text in this file would break the parser. Non-ASCII *data* (the auto-title
# sample) is therefore built from [char] codes below.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage2.ps1
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage2.ps1 -BaseUrl http://127.0.0.1:18080
#
# Preconditions:
#   1. MySQL ai_platform has the gateway tables (deploy/mysql/001_gateway_tables.sql)
#   2. test accounts exist (deploy/mysql/002_test_accounts.sql)
#   3. gateway is running with ENV_FILE pointing at ai-platform-go\.env
#   4. the port is really free: a STALE process from the previous round makes
#      "all green" fake. Check before starting:
#        Get-NetTCPConnection -LocalPort 18080 -State Listen
#      Port 8080 is NOT usable on this machine (invisible Spring service), which
#      is why the default here and in stage1/stage3/stage4 is 18080.
#
# M2 fact of life: the orchestrator is not wired yet, so
# POST /conversations/{id}/messages persists the user message (seq allocated)
# and then asks the AI for an answer. **From M3 the orchestrator is wired in**,
# so a send answers 200 and ALSO persists the assistant message: every
# successful send adds TWO rows (odd seq = user, even seq = assistant), and
# every "exactly N messages" assertion below counts in pairs.
#
# In M2 the same call answered 503 AI_UNAVAILABLE and left no assistant row.
# That branch still exists in the gateway, but it is now only reachable when
# the AI side is genuinely unreachable -- so the assertions here were flipped
# to SendStatus/MsgsPerSend instead of being deleted. Anything that still
# expects 503 must say WHY (e.g. a request rejected before the AI call).
# =============================================================================

param(
    # Default port is 18080, NOT 8080: see tools\curl_stage1.ps1 header.
    [string]$BaseUrl = "http://127.0.0.1:18080",
    [string]$Stage2Email = "stage2@test.local",
    [string]$Stage2Password = "Stage2#Test2026",
    # User B: only used to prove cross-user access returns 404 (AC-CONV-02).
    [string]$Stage1Email = "stage1@test.local",
    [string]$Stage1Password = "Stage1#Test2026"
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:Passed = 0
$script:Failed = 0

# M3: 编排已接通 -> 一次 send 返回 200，并且**同时**落库 assistant 消息。
$script:SendStatus = 200
$script:MsgsPerSend = 2

# 这里曾经有个 $script:AIUnavailable = 503：M2 时期每次 send 都是 503。
# 现在它不再是**任何**用例的期望值，所以变量删掉了 —— 留着会让人以为还有
# 负向用例在跑。「AI 侧不可用 -> 503」这条降级路径改为在单测里覆盖：
#   internal/biz/ai_proxy_test.go        （非信封响应归一化 / 不可达）
#   internal/biz/orchestration_test.go   （上游错误信封原样透出）
# 实机脚本里没有确定性触发它的办法：要触发就得把 AI 侧停掉，而「脚本结果
# 取决于环境里另一个进程在不在」正是最难查的那类假失败。

function Write-Section([string]$title) {
    Write-Host ""
    Write-Host ("=== " + $title + " ===") -ForegroundColor Cyan
}

function Check([string]$name, [bool]$ok, [string]$detail) {
    if ($ok) {
        $script:Passed++
        Write-Host ("  [PASS] " + $name) -ForegroundColor Green
    } else {
        $script:Failed++
        Write-Host ("  [FAIL] " + $name + " :: " + $detail) -ForegroundColor Red
    }
}

function Check-Equal([string]$name, $actual, $expected) {
    Check $name ($actual -eq $expected) ("expected=" + $expected + " actual=" + $actual)
}

function Check-Match([string]$name, [string]$actual, [string]$pattern) {
    Check $name ($actual -match $pattern) ("pattern=" + $pattern + " actual=" + $actual)
}

# Item-Count exists because @($null) is a ONE element array in PowerShell:
# plain "@($x).Count" would report 1 for a JSON null and silently turn an
# "empty list" assertion into a false pass.
function Item-Count($value) {
    if ($null -eq $value) { return 0 }
    return @($value).Count
}

# Get-JsonLeafMap flattens a parsed JSON value into "path=scalar" lines so two
# bodies can be compared by VALUE rather than by bytes.
#
# Containers emit a "path=<obj:N>" / "path=<arr:N>" marker *in addition to*
# recursing, which is not redundant: without it every EMPTY container is
# invisible, so "kb_ids":[] and "kb_ids":{} would compare as equal -- an
# assertion that quietly passes on a wrong response. The marker also makes an
# array length change show up as its own line.
#
# Arrays get indexed paths (items[0].id), which is deliberate: it checks the
# element ORDER too, not just the set of values.
# Type test note: ConvertFrom-Json in PS 5.1 produces
# System.Management.Automation.PSCustomObject for objects and Object[] for
# arrays, so the dispatch below is exact (no duck typing needed).
function Get-JsonLeafMap($value, [string]$path) {
    if ($null -eq $value) {
        # PS cannot tell a JSON null from an absent member; for the bodies we
        # compare ("last_message_at": null) a null leaf IS a real leaf.
        return @($path + "=<null>")
    }
    if ($value -is [System.Management.Automation.PSCustomObject]) {
        $props = @($value.PSObject.Properties)
        $out = @($path + "=<obj:" + $props.Count + ">")
        foreach ($p in $props) {
            $out += Get-JsonLeafMap $p.Value ($path + "." + $p.Name)
        }
        return @($out)
    }
    if ($value -is [System.Array]) {
        $out = @($path + "=<arr:" + $value.Length + ">")
        for ($i = 0; $i -lt $value.Length; $i++) {
            $out += Get-JsonLeafMap $value[$i] ($path + "[" + $i + "]")
        }
        return @($out)
    }
    return @($path + "=" + [string]$value)
}

# Compare-JsonBody returns "" when both bodies hold the SAME JSON VALUE,
# otherwise a "left-only=... right-only=..." diff.
#
# Why byte equality is the WRONG assertion for a replayed response: the
# snapshot lives in the shared table's response_body column, which is a native
# MySQL JSON type (docs/05 section 2.7). MySQL normalises a JSON document on
# write (object keys reordered, a space added after every , and :) and
# re-serialises it on read, so the replayed body is semantically identical but
# NOT byte identical to the first response. Measured, same body both ways:
#   in  {"seq":3,"a":1,"b":{"y":2,"x":1}}
#   out {"a": 1, "b": {"x": 1, "y": 2}, "seq": 3}
# docs/02 section 7 promises "the first response is replayed (including its
# status code)" -- a JSON client sees exactly the same value, which is what
# this asserts. A byte-level assertion here would fail on a correct server and
# send the next reader hunting for a bug that does not exist.
function Compare-JsonBody([string]$left, [string]$right) {
    try { $a = ConvertFrom-Json $left } catch { return "left side is not valid JSON: " + $_.Exception.Message }
    try { $b = ConvertFrom-Json $right } catch { return "right side is not valid JSON: " + $_.Exception.Message }
    $la = @(Get-JsonLeafMap $a '$' | Sort-Object)
    $lb = @(Get-JsonLeafMap $b '$' | Sort-Object)
    $diff = @(Compare-Object $la $lb)
    if ($diff.Count -eq 0) { return "" }
    $onlyLeft = @($diff | Where-Object { $_.SideIndicator -eq '<=' } | ForEach-Object { $_.InputObject })
    $onlyRight = @($diff | Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object { $_.InputObject })
    return ("left-only=" + (Truncate-Body ($onlyLeft -join "; ")) + " right-only=" + (Truncate-Body ($onlyRight -join "; ")))
}

function Check-JsonEqual([string]$name, [string]$left, [string]$right) {
    $diff = Compare-JsonBody $left $right
    Check $name ($diff -eq "") $diff
}

# ---- Kill the harness's own floor: turn the system proxy off ----
#
# With a local proxy resident (Clash etc. at 127.0.0.1:7897), PS 5.1's
# `Invoke-WebRequest` sends even **loopback** addresses through proxy resolution.
# Measured on this machine: the same `/health` -> IWR times out after 10s while
# `curl.exe --noproxy '*'` answers in 412ms (run_all_stages.ps1 header, docs/09
# section 2.1). Every request in this script targets 127.0.0.1, so disabling the
# proxy is pure profit -- it removes a **per-round-trip** fixed cost. This is the
# heaviest stage of the whole suite: 96 `Invoke-Api` calls in M2 alone.
#
# Why the .NET static instead of only `-Proxy $null`: PS 5.1's `-Proxy` ends up in
# `if (Proxy != null) { request.Proxy = Proxy }`, so passing `$null` is equivalent
# to **not passing it** on some builds (the proxy stays active). Setting the
# process-wide default is the part that deterministically takes effect; Invoke-Api
# also carries `-Proxy $null` (or `-NoProxy` on PS 6+) as a second line of defence.
#
# (ASCII only on purpose: this file has no BOM and PS 5.1 would parse non-ASCII as
# GBK -- see the note in curl_stage1.ps1.)
#
# WARNING: this assumes every target is loopback. If you point -BaseUrl at a
# non-loopback address (a remote gateway behind a corporate proxy), delete this.
[System.Net.WebRequest]::DefaultWebProxy = $null

# Invoke-Api returns a hashtable: Status / Body / Json / Headers / Raw.
# Never throws on 4xx/5xx: the error envelope itself is what we assert on.
function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [string]$Token = $null,
        [string]$ContentType = "application/json; charset=utf-8",
        [hashtable]$ExtraHeaders = $null,
        [switch]$SkipJson
    )

    $reqHeaders = @{}
    if ($Token) { $reqHeaders['Authorization'] = "Bearer $Token" }
    if ($ExtraHeaders) {
        foreach ($k in $ExtraHeaders.Keys) { $reqHeaders[$k] = $ExtraHeaders[$k] }
    }

    $reqArgs = @{
        Method          = $Method
        Uri             = ($BaseUrl + $Path)
        Headers         = $reqHeaders
        UseBasicParsing = $true
        # 120s：M2 时期一次 send 立刻返回 503，30s 绰绰有余；M3 起 send 要真的
        # 等模型出话（实测 0.9s ~ 64s 都有）。客户端先超时的话 status 会变成 0，
        # 断言会报成「接口返回 0」——一个指向完全错误方向的假失败。
        TimeoutSec      = 120
    }
    if ($null -ne $Body) {
        if ($Body -is [string]) {
            $payload = $Body
        } else {
            $payload = ($Body | ConvertTo-Json -Depth 8 -Compress)
        }
        # Encode explicitly: PS 5.1 would otherwise send the body as
        # ISO-8859-1 and mangle every non-ASCII character.
        $reqArgs['Body'] = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $reqArgs['ContentType'] = $ContentType
    }

    # Second line of defence next to the process-wide DefaultWebProxy = $null above:
    # PS 6+ has -NoProxy, PS 5.1 has only -Proxy (see the note at the top of file).
    if ($PSVersionTable.PSVersion.Major -ge 6) { $reqArgs["NoProxy"] = $true } else { $reqArgs["Proxy"] = $null }

    $status = 0
    $content = ""
    $respHeaders = $null
    try {
        $resp = Invoke-WebRequest @reqArgs
        $status = [int]$resp.StatusCode
        $content = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        $respHeaders = $resp.Headers
    } catch {
        # PS 5.1 has no -SkipHttpErrorCheck (PS 7+) and the response stream on
        # the WebException is already consumed. $_.ErrorDetails.Message is the
        # supported way to get the 4xx/5xx body.
        $webResp = $null
        if ($_.Exception -and $_.Exception.PSObject.Properties['Response']) {
            $webResp = $_.Exception.Response
        }
        if ($null -ne $webResp) {
            $status = [int]$webResp.StatusCode
            $respHeaders = $webResp.Headers
        }
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $content = [string]$_.ErrorDetails.Message
        }
        if (-not $content -and ($null -ne $webResp)) {
            try {
                $stream = $webResp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                $content = $reader.ReadToEnd()
                $reader.Dispose()
            } catch { $content = "" }
        }
        if ($status -eq 0) {
            return @{ Status = 0; Body = ""; Json = $null; Headers = $null; Error = $_.Exception.Message }
        }
    }

    $json = $null
    if (-not $SkipJson -and $content) {
        try { $json = $content | ConvertFrom-Json } catch { $json = $null }
    }
    return @{ Status = $status; Body = $content; Json = $json; Headers = $respHeaders; Error = "" }
}

# Invoke-WebRequest returns a System.Net.WebHeaderCollection, which has neither
# ContainsKey nor Keys -- so header access goes through these helpers.
function Get-Header($headers, [string]$name) {
    if ($null -eq $headers) { return "" }
    if ($headers -is [System.Collections.IDictionary]) {
        if ($headers.ContainsKey($name)) { return [string]$headers[$name] }
        return ""
    }
    foreach ($k in $headers.AllKeys) { if ($k -ieq $name) { return [string]$headers[$k] } }
    return ""
}

function Error-Code($resp) {
    if ($resp.Json -and $resp.Json.error -and $resp.Json.error.code) { return [string]$resp.Json.error.code }
    return ""
}

function Field-Error($resp, [int]$index = 0) {
    if ($null -eq $resp.Json) { return $null }
    if ($null -eq $resp.Json.error) { return $null }
    if ($null -eq $resp.Json.error.details) { return $null }
    $fields = @($resp.Json.error.details.fields)
    if ($fields.Count -le $index) { return $null }
    return $fields[$index]
}

function Truncate-Body([string]$s, [int]$max = 260) {
    if ($null -eq $s) { return "" }
    $s = $s -replace "\s+", " "
    if ($s.Length -le $max) { return $s }
    return $s.Substring(0, $max) + "..."
}

# Assert-JsonComparerWorks is a guard on the guard.
#
# Compare-JsonBody backs exactly one assertion (H2b), and its failure mode is
# the bad one: a comparer that answers "equal" for everything turns H2b into a
# permanent false pass -- exactly the class of bug this suite exists to catch.
# Running a few known cases through it every time makes that visibly red.
#
# Case choice is deliberate: one must be EQUAL only because of MySQL's JSON
# normalisation (key order + whitespace), and the container cases exist because
# a leaf-only walk cannot see "kb_ids":[] vs "kb_ids":{}.
function Assert-JsonComparerWorks {
    $equal = @(
        @{ a = '{"a":1,"b":{"y":2,"x":1}}'; b = '{"a": 1, "b": {"x": 1, "y": 2}}' },
        @{ a = '{"kb_ids":[]}'; b = '{"kb_ids": []}' },
        @{ a = '{"t":"2026-09-29T07:48:18.129Z"}'; b = '{"t": "2026-09-29T07:48:18.129Z"}' },
        @{ a = '{"items":[{"id":"a","seq":1}]}'; b = '{"items": [{"id": "a", "seq": 1}]}' }
    )
    for ($i = 0; $i -lt $equal.Count; $i++) {
        Check ("SELF0." + $i + " comparer: spacing/key order must NOT count as a difference") `
            ((Compare-JsonBody $equal[$i].a $equal[$i].b) -eq "") "reported a diff"
    }

    $different = @(
        @{ a = '{"n":1}'; b = '{"n":2}' },
        @{ a = '{"x":[1,2]}'; b = '{"x":[2,1]}' },
        @{ a = '{"x":[1,2]}'; b = '{"x":[1,2,3]}' },
        @{ a = '{}'; b = '[]' },
        @{ a = '{"kb_ids":[]}'; b = '{"kb_ids":{}}' },
        @{ a = '{"a":1}'; b = '{"a":1,"b":2}' },
        @{ a = '{"a":null}'; b = '{"a":0}' },
        @{ a = 'not json'; b = '{}' }
    )
    for ($i = 0; $i -lt $different.Count; $i++) {
        Check ("SELF1." + $i + " comparer: a real difference must be reported") `
            ((Compare-JsonBody $different[$i].a $different[$i].b) -ne "") "reported equal"
    }
}

# -----------------------------------------------------------------------------
# A. login / cleanup
# -----------------------------------------------------------------------------
Write-Section "A. login and cleanup"

# Self-check first: if the JSON comparer is broken, H2b below is meaningless.
Assert-JsonComparerWorks

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $Stage2Email; password = $Stage2Password }
Check-Equal "A1 login as stage2 -> 200" $r.Status 200
$tokenA = [string]$r.Json.access_token
Check "A1b access token present" ($tokenA.Length -gt 20) (Truncate-Body $r.Body)

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $Stage1Email; password = $Stage1Password }
Check-Equal "A2 login as stage1 (user B) -> 200" $r.Status 200
$tokenB = [string]$r.Json.access_token
Check "A2b user B token present" ($tokenB.Length -gt 20) (Truncate-Body $r.Body)

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $tokenA
$userAID = [string]$r.Json.id
Check "A3 GET /me works and returns our user id" ($userAID.StartsWith("u_")) ("id=" + $userAID)

# Make the run repeatable: conversations from the previous round would break
# every "exactly N items" assertion below. Soft-deleted rows are excluded from
# the list, so deleting what we can see is enough.
$cleanupDeleted = 0
$cleanupCursor = ""
do {
    $path = "/api/v1/conversations?limit=100"
    if ($cleanupCursor) { $path += "&cursor=" + [uri]::EscapeDataString($cleanupCursor) }
    $page = Invoke-Api -Method GET -Path $path -Token $tokenA
    foreach ($c in @($page.Json.items)) {
        $del = Invoke-Api -Method DELETE -Path ("/api/v1/conversations/" + $c.id) -Token $tokenA
        if ($del.Status -eq 204) { $cleanupDeleted++ }
    }
    if ([string]$page.Json.has_more -eq "True") {
        $cleanupCursor = [string]$page.Json.next_cursor
    } else {
        $cleanupCursor = ""
    }
} while ($cleanupCursor)
Write-Host ("  (cleanup: removed " + $cleanupDeleted + " conversation(s) from previous rounds)") -ForegroundColor DarkGray

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=100" -Token $tokenA
Check-Equal "A4 baseline is empty after cleanup" (Item-Count $r.Json.items) 0
# Single quotes: backslash is NOT an escape character in PowerShell, so a
# double-quoted '\"' would compare against a literal backslash-quote.
Check-Equal "A4b empty list is [] not null" $r.Body '{"items":[],"next_cursor":null,"has_more":false}'

# -----------------------------------------------------------------------------
# B. create / get  (AC-CONV-01 / AC-CONV-02)
# -----------------------------------------------------------------------------
Write-Section "B. create and get (AC-CONV-01 / AC-CONV-02)"

$forgedID = "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"
$createBody = '{"id":"' + $forgedID + '","title":"stage2 probe","metadata":{"scene":"stage2"}}'
$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body $createBody -Token $tokenA
Check-Equal "B1 POST /conversations -> 201" $r.Status 201
$convA = [string]$r.Json.id
Check-Match "B1b id matches cv_ + 26 char Crockford ULID (AC-CONV-01)" $convA '^cv_[0-9A-HJKMNP-TV-Z]{26}$'
Check "B1c client supplied id is ignored (AC-CONV-01)" ($convA -ne $forgedID) ("body id=" + $forgedID + " got=" + $convA)
Check-Equal "B1d Location header points at the new resource" (Get-Header $r.Headers 'Location') ("/api/v1/conversations/" + $convA)
Check-Equal "B1e status = active" ([string]$r.Json.status) "active"
Check-Equal "B1f title_source = auto" ([string]$r.Json.title_source) "auto"
Check-Equal "B1g title kept" ([string]$r.Json.title) "stage2 probe"
Check-Equal "B1h user_id = caller" ([string]$r.Json.user_id) $userAID
Check-Equal "B1i message_count = 0" ([int]$r.Json.message_count) 0
Check "B1j last_message_at is null" ($null -eq $r.Json.last_message_at) ("actual=" + [string]$r.Json.last_message_at)
Check-Equal "B1k kb_ids is an empty array" (Item-Count $r.Json.kb_ids) 0
Check-Equal "B1l pinned = false" ([string]$r.Json.pinned) "False"
Check-Equal "B1m metadata round-trips" ([string]$r.Json.metadata.scene) "stage2"
Check "B1n timestamps are RFC3339 with milliseconds" `
    (([string]$r.Json.created_at) -match '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$') `
    ("created_at=" + [string]$r.Json.created_at)
$leaked = $r.Body
Check "B1o no soft-delete/secret column leaks" (-not ($leaked -match '(?i)(deleted_at|password|token_version)')) (Truncate-Body $leaked)

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convA) -Token $tokenA
Check-Equal "B2 GET /conversations/{id} -> 200" $r.Status 200
Check-Equal "B2b same id" ([string]$r.Json.id) $convA

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convA) -Token $tokenB
Check-Equal "B3 user B reading user A conversation -> 404 (AC-CONV-02)" $r.Status 404
Check-Equal "B3b code = CONVERSATION_NOT_FOUND" (Error-Code $r) "CONVERSATION_NOT_FOUND"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C" -Token $tokenA
Check-Equal "B4 unknown but well-formed id -> 404" $r.Status 404
Check-Equal "B4b code = CONVERSATION_NOT_FOUND" (Error-Code $r) "CONVERSATION_NOT_FOUND"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body @{ title = "x" } -Token $tokenB
Check-Equal "B5 user B PATCHing user A conversation -> 404" $r.Status 404

$r = Invoke-Api -Method DELETE -Path ("/api/v1/conversations/" + $convA) -Token $tokenB
Check-Equal "B6 user B DELETEing user A conversation -> 404 (not 204)" $r.Status 404
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convA) -Token $tokenA
Check-Equal "B6b the conversation is still there" $r.Status 200

# -----------------------------------------------------------------------------
# C. update / patch tri-state
# -----------------------------------------------------------------------------
Write-Section "C. update (PATCH)"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{"title":"  renamed  "}' -Token $tokenA
Check-Equal "C1 PATCH title -> 200" $r.Status 200
Check-Equal "C1b title trimmed" ([string]$r.Json.title) "renamed"
Check-Equal "C1c title_source became manual (AC-CONV-04)" ([string]$r.Json.title_source) "manual"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{"model":"deepseek-flash"}' -Token $tokenA
Check-Equal "C2 PATCH model -> 200" $r.Status 200
Check-Equal "C2b model stored" ([string]$r.Json.model) "deepseek-flash"

# model:null is *valid* semantics (fall back to the global default model), which
# is exactly why the field needs three states instead of a plain pointer.
$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{"model":null}' -Token $tokenA
Check-Equal "C3 PATCH model=null -> 200" $r.Status 200
Check "C3b model cleared" ($null -eq $r.Json.model) ("actual=" + [string]$r.Json.model)

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{}' -Token $tokenA
Check-Equal "C4 PATCH with no fields is idempotent -> 200" $r.Status 200
Check-Equal "C4b response echoes current state" ([string]$r.Json.title) "renamed"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body ('{"title":"' + ("t" * 101) + '"}') -Token $tokenA
Check-Equal "C5 title longer than 100 runes -> 400" $r.Status 400
Check-Equal "C5b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"
Check-Equal "C5c field = title" ([string](Field-Error $r).field) "title"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{"model":""}' -Token $tokenA
Check-Equal "C6 model empty string -> 400 (use null to clear)" $r.Status 400
Check-Equal "C6b field = model" ([string](Field-Error $r).field) "model"

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convA) -Body '{"kb_ids":["not-a-ulid"]}' -Token $tokenA
Check-Equal "C7 malformed kb_ids -> 400" $r.Status 400
Check-Equal "C7b field = kb_ids" ([string](Field-Error $r).field) "kb_ids"

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"metadata":{"kk":"vv"},"kb_ids":[]}' -Token $tokenA
Check-Equal "C8 create with metadata only -> 201" $r.Status 201
Check-Equal "C8b title stays empty (auto title pending)" ([string]$r.Json.title) ""

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":"a","id":"cv_bad"}' -Token $tokenA
Check-Equal "C9 unknown field in body is ignored -> 201" $r.Status 201

# -----------------------------------------------------------------------------
# D. auto title  (AC-CONV-03 / AC-CONV-04)
# -----------------------------------------------------------------------------
Write-Section "D. auto title (AC-CONV-03 / AC-CONV-04)"

# Build the non-ASCII sample from code points: this file must stay ASCII-only.
# The sample is exactly the one from AC-CONV-03: two spaces, the two CJK
# characters 0x4F60 0x597D (ni hao), a blank line, 0x4E16 0x754C (shi jie),
# then two spaces. Expected title: "ni hao shi jie" with the newlines turned
# into a single collapsed space. (The characters are spelled as code points
# because writing them literally here would make this file non-ASCII.)
$nl = [string][char]10
$hello = ([string][char]0x4F60) + ([string][char]0x597D)
$world = ([string][char]0x4E16) + ([string][char]0x754C)
$sampleContent = "  " + $hello + $nl + $nl + $world + "  "
$expectTitle = $hello + " " + $world

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":""}' -Token $tokenA
Check-Equal "D0 fresh conversation created -> 201" $r.Status 201
$convTitle = [string]$r.Json.id

# M3: the user message is persisted (seq allocated), the AI is asked, and the
# assistant message is persisted too -- so the call answers 200 and the
# conversation now holds TWO rows. The title must already be applied.
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body @{ content = $sampleContent } -Token $tokenA
Check-Equal "D1 send on M3 -> 200" $r.Status $script:SendStatus
Check-Equal "D1b user message is seq 1 / role user" `
    (([int]$r.Json.user_message.seq).ToString() + "/" + [string]$r.Json.user_message.role) "1/user"
Check "D1c assistant_message is present" ($null -ne $r.Json.assistant_message) ("body=" + (Truncate-Body $r.Body))
Check "D1d assistant answer is not empty" `
    (([string]$r.Json.assistant_message.content).Length -gt 0) ("body=" + (Truncate-Body $r.Body))
Check-Equal "D1e assistant message is seq 2 / role assistant" `
    (([int]$r.Json.assistant_message.seq).ToString() + "/" + [string]$r.Json.assistant_message.role) "2/assistant"
Check "D1f model is filled in from the AI response" `
    (([string]$r.Json.assistant_message.model).Length -gt 0) ("model=" + [string]$r.Json.assistant_message.model)
Check "D1g elapsed_ms is a positive number" ([int]$r.Json.assistant_message.elapsed_ms -gt 0) `
    ("elapsed_ms=" + [string]$r.Json.assistant_message.elapsed_ms)

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle) -Token $tokenA
Check-Equal "D2 title derived from first message (AC-CONV-03)" ([string]$r.Json.title) $expectTitle
Check-Equal "D2b title_source = auto" ([string]$r.Json.title_source) "auto"
Check-Equal "D2c message_count = 2 (user + assistant)" ([int]$r.Json.message_count) $script:MsgsPerSend
Check "D2d last_message_at is set" ($null -ne $r.Json.last_message_at) ("actual=" + [string]$r.Json.last_message_at)

$r = Invoke-Api -Method PATCH -Path ("/api/v1/conversations/" + $convTitle) -Body '{"title":"manual title"}' -Token $tokenA
Check-Equal "D3 manual PATCH title -> 200" $r.Status 200
Check-Equal "D3b title_source = manual" ([string]$r.Json.title_source) "manual"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body @{ content = "second question" } -Token $tokenA
Check-Equal "D4 second send -> 200" $r.Status $script:SendStatus
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle) -Token $tokenA
Check-Equal "D5 manual title is not overwritten (AC-CONV-04)" ([string]$r.Json.title) "manual title"
Check-Equal "D5b title_source stays manual" ([string]$r.Json.title_source) "manual"
Check-Equal "D5c message_count = 4 (two rounds)" ([int]$r.Json.message_count) (2 * $script:MsgsPerSend)

# -----------------------------------------------------------------------------
# E. archive / unarchive  (AC-CONV-05)
# -----------------------------------------------------------------------------
Write-Section "E. archive (AC-CONV-05)"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/archive") -Token $tokenA
Check-Equal "E1 archive -> 200" $r.Status 200
Check-Equal "E1b status = archived" ([string]$r.Json.status) "archived"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body @{ content = "while archived" } -Token $tokenA
Check-Equal "E2 send to archived conversation -> 409 (AC-CONV-05)" $r.Status 409
Check-Equal "E2b code = CONVERSATION_ARCHIVED" (Error-Code $r) "CONVERSATION_ARCHIVED"

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Token $tokenA
Check-Equal "E3 archived conversation is still readable -> 200" $r.Status 200
# 被拒的那次 send 一条都不许落库：两条来自 D1/D4 两轮，而不是 2 条 user 消息。
Check-Equal "E3b no message was written (still 2 rounds)" (Item-Count $r.Json.items) (2 * $script:MsgsPerSend)

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?status=archived&limit=100" -Token $tokenA
Check-Equal "E4 status filter finds the archived one" (Item-Count $r.Json.items) 1

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/unarchive") -Token $tokenA
Check-Equal "E5 unarchive -> 200" $r.Status 200
Check-Equal "E5b status = active again" ([string]$r.Json.status) "active"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body @{ content = "after unarchive" } -Token $tokenA
Check-Equal "E6 send works again after unarchive (AC-CONV-05)" $r.Status $script:SendStatus

# Unarchiving an already active conversation must not be mistaken for "not
# found": MySQL reports 0 affected rows when an UPDATE stores the same value.
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/unarchive") -Token $tokenA
Check-Equal "E7 unarchive twice -> 200 (0 affected rows is not 404)" $r.Status 200

# -----------------------------------------------------------------------------
# F. messages: list / order / get / delete
# -----------------------------------------------------------------------------
Write-Section "F. messages"

# This conversation has had three successful sends (D1, D4, E6), i.e. six rows.
# The seq assertion is written as "equals 1..N" rather than a literal because
# that IS the AC-CONV-07 invariant (no gap, no repeat); the expected COUNT is
# asserted separately so a wrong number cannot hide behind the invariant.
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?order=asc&limit=100") -Token $tokenA
Check-Equal "F1 GET messages -> 200" $r.Status 200
$ascSeq = @()
foreach ($m in @($r.Json.items)) { $ascSeq += [int]$m.seq }
Check-Equal "F1a exactly 3 rounds = 6 messages" $ascSeq.Count (3 * $script:MsgsPerSend)
Check-Equal "F1b seq ascends 1..N with no gap or repeat" ($ascSeq -join ",") ((1..$ascSeq.Count) -join ",")
Check-Equal "F1c roles alternate user/assistant (M3 persists both)" `
    ((@($r.Json.items) | ForEach-Object { [string]$_.role }) -join ",") "user,assistant,user,assistant,user,assistant"
Check-Equal "F1d content of the first message is trimmed" ([string]$r.Json.items[0].content) ($hello + $nl + $nl + $world)
Check-Equal "F1e references defaults to []" (Item-Count $r.Json.items[0].references) 0
Check-Equal "F1f tool_calls defaults to []" (Item-Count $r.Json.items[0].tool_calls) 0
Check-Equal "F1g status = completed" ([string]$r.Json.items[0].status) "completed"
Check "F1h usage is null for a user message" ($null -eq $r.Json.items[0].usage) (Truncate-Body $r.Body)
Check "F1i degraded_reasons defaults to []" ($null -ne $r.Json.items[0].degraded_reasons) (Truncate-Body $r.Body)

$msgID = [string]$r.Json.items[0].id
Check-Match "F1j message id matches msg_ + ULID" $msgID '^msg_[0-9A-HJKMNP-TV-Z]{26}$'

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?order=desc&limit=100") -Token $tokenA
$descSeq = @()
foreach ($m in @($r.Json.items)) { $descSeq += [int]$m.seq }
Check-Equal "F2 order=desc -> N..1" ($descSeq -join ",") (($ascSeq.Count..1) -join ",")

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Token $tokenA
$defSeq = @()
foreach ($m in @($r.Json.items)) { $defSeq += [int]$m.seq }
Check-Equal "F3 default order is desc (contract default)" ($defSeq -join ",") (($ascSeq.Count..1) -join ",")

$r = Invoke-Api -Method GET -Path ("/api/v1/messages/" + $msgID) -Token $tokenA
Check-Equal "F4 GET /messages/{id} -> 200" $r.Status 200
Check-Equal "F4b same message" ([string]$r.Json.id) $msgID
Check-Equal "F4c conversation_id is present" ([string]$r.Json.conversation_id) $convTitle

# Malformed message cursor: same reasoning as J7 (a 400, never a 500).
# Checked here, while this conversation still exists: section J wipes the
# baseline first, and a deleted conversation would answer 404 instead.
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?cursor=not-a-cursor") -Token $tokenA
Check-Equal "F4d malformed message cursor -> 400 (not 500)" $r.Status 400

$r = Invoke-Api -Method GET -Path ("/api/v1/messages/" + $msgID) -Token $tokenB
Check-Equal "F5 user B reading user A message -> 404" $r.Status 404
Check-Equal "F5b code = MESSAGE_NOT_FOUND" (Error-Code $r) "MESSAGE_NOT_FOUND"

$r = Invoke-Api -Method GET -Path "/api/v1/messages/msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3C" -Token $tokenA
Check-Equal "F6 unknown message -> 404 MESSAGE_NOT_FOUND" (Error-Code $r) "MESSAGE_NOT_FOUND"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C/messages" -Token $tokenA
Check-Equal "F7 listing messages of an unknown conversation -> 404 (not an empty list)" $r.Status 404
Check-Equal "F7b code = CONVERSATION_NOT_FOUND" (Error-Code $r) "CONVERSATION_NOT_FOUND"

# Validation of the send body.
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body '{"content":""}' -Token $tokenA
Check-Equal "F8 empty content -> 400" $r.Status 400
Check-Equal "F8b field = content" ([string](Field-Error $r).field) "content"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body '{"content":"   "}' -Token $tokenA
Check-Equal "F9 whitespace-only content -> 400" $r.Status 400

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body ('{"content":"' + ("c" * 8001) + '"}') -Token $tokenA
Check-Equal "F10 content longer than 8000 runes -> 400" $r.Status 400
Check-Equal "F10b reason = too_long" ([string](Field-Error $r).reason) "too_long"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") `
    -Body @{ content = "hi"; attachments = @(@{ doc_id = "doc_01J8ZQ3K7N9P2V6R4T8W1Y5B3C" }) } -Token $tokenA
Check-Equal "F11 attachments are rejected, not ignored -> 400" $r.Status 400
Check-Equal "F11b field = attachments" ([string](Field-Error $r).field) "attachments"
Check-Equal "F11c reason = not_supported" ([string](Field-Error $r).reason) "not_supported"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body '{"content":"hi","temperature":3}' -Token $tokenA
Check-Equal "F12 temperature out of range -> 400" $r.Status 400
Check-Equal "F12b field = temperature" ([string](Field-Error $r).field) "temperature"

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convTitle + "/messages") -Body '{"content":"hi","top_k":0}' -Token $tokenA
Check-Equal "F13 top_k = 0 -> 400" $r.Status 400

$r = Invoke-Api -Method POST -Path "/api/v1/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C/messages" -Body '{"content":"hi"}' -Token $tokenA
Check-Equal "F14 send to unknown conversation -> 404" $r.Status 404

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?limit=101") -Token $tokenA
Check-Equal "F15 limit above the cap -> 400 (never silently truncated)" $r.Status 400
Check-Equal "F15b field = limit" ([string](Field-Error $r).field) "limit"

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?limit=abc") -Token $tokenA
Check-Equal "F16 non-numeric limit -> 400" $r.Status 400

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convTitle + "/messages?order=newest") -Token $tokenA
Check-Equal "F17 unknown order -> 400" $r.Status 400
Check-Equal "F17b field = order" ([string](Field-Error $r).field) "order"

# Delete one message: the ledger is append-mostly, but the contract allows it.
$r = Invoke-Api -Method DELETE -Path ("/api/v1/messages/" + $msgID) -Token $tokenA
Check-Equal "F18 DELETE /messages/{id} -> 204" $r.Status 204
Check-Equal "F18b body is empty" $r.Body ""
$r = Invoke-Api -Method GET -Path ("/api/v1/messages/" + $msgID) -Token $tokenA
Check-Equal "F19 deleted message -> 404" $r.Status 404
$r = Invoke-Api -Method DELETE -Path ("/api/v1/messages/" + $msgID) -Token $tokenA
Check-Equal "F20 deleting it twice -> 404 (not 204)" $r.Status 404

# -----------------------------------------------------------------------------
# G. concurrent sends -> seq must be unique (AC-CONV-07)
# -----------------------------------------------------------------------------
Write-Section "G. concurrent sends and seq uniqueness (AC-CONV-07)"

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":"concurrency"}' -Token $tokenA
$convRace = [string]$r.Json.id
Check-Equal "G0 conversation created -> 201" $r.Status 201

# Five parallel requests: this is what LAST_INSERT_ID(message_count + 1) inside
# the same transaction has to survive. Jobs are used instead of a sequential
# loop because the sequential version cannot fail here at all.
$jobs = @()
foreach ($i in 1..5) {
    $jobs += Start-Job -ScriptBlock {
        param($base, $token, $cid, $n)
        $body = '{"content":"concurrent message ' + $n + '"}'
        try {
            $resp = Invoke-WebRequest -Method POST -Uri ($base + "/api/v1/conversations/" + $cid + "/messages") `
                -Headers @{ Authorization = "Bearer $token" } `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) `
                -ContentType "application/json; charset=utf-8" -UseBasicParsing -TimeoutSec 180
            return [int]$resp.StatusCode
        } catch {
            if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
            return 0
        }
    } -ArgumentList $BaseUrl, $tokenA, $convRace, $i
}
$raceStatuses = @($jobs | Wait-Job | Receive-Job)
$jobs | Remove-Job -Force
Check-Equal "G1 five concurrent sends all answered 200" `
    (($raceStatuses | Where-Object { $_ -eq $script:SendStatus } | Measure-Object).Count) 5

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convRace + "/messages?order=asc&limit=100") -Token $tokenA
$raceSeq = @()
foreach ($m in @($r.Json.items)) { $raceSeq += [int]$m.seq }
Check-Equal "G2 exactly 5 rounds = 10 messages landed (no duplicates)" $raceSeq.Count (5 * $script:MsgsPerSend)
Check-Equal "G3 seq is exactly 1..10 with no gap or repeat (AC-CONV-07)" `
    ($raceSeq -join ",") ((1..(5 * $script:MsgsPerSend)) -join ",")

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convRace) -Token $tokenA
Check-Equal "G4 message_count matches the ledger" ([int]$r.Json.message_count) (5 * $script:MsgsPerSend)

# -----------------------------------------------------------------------------
# H. idempotency
# -----------------------------------------------------------------------------
Write-Section "H. idempotency (docs/02 section 7)"

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
$idemKey = "stage2-idem-" + $stamp + "-a"
$idemBody = '{"title":"idempotent create"}'

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=100" -Token $tokenA
$beforeCount = Item-Count $r.Json.items

$r1 = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body $idemBody -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = $idemKey }
Check-Equal "H1 first request with Idempotency-Key -> 201" $r1.Status 201
$idemConvID = [string]$r1.Json.id
Check "H1b first response is not marked as replayed" ((Get-Header $r1.Headers 'Idempotency-Replayed') -eq "") ("header=" + (Get-Header $r1.Headers 'Idempotency-Replayed'))

$r2 = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body $idemBody -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = $idemKey }
Check-Equal "H2 replay -> same status 201" $r2.Status 201
# Deep (value) equality, not byte equality -- see Compare-JsonBody above.
Check-JsonEqual "H2b replay -> same JSON value as the first response" $r1.Body $r2.Body
Check-Equal "H2c replay -> same conversation id" ([string]$r2.Json.id) $idemConvID
Check-Equal "H2d Idempotency-Replayed: true" (Get-Header $r2.Headers 'Idempotency-Replayed') "true"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=100" -Token $tokenA
Check-Equal "H3 replay created no extra conversation" (Item-Count $r.Json.items) ($beforeCount + 1)

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":"different body"}' -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = $idemKey }
Check-Equal "H4 same key + different body -> 409" $r.Status 409
Check-Equal "H4b code = CONFLICT" (Error-Code $r) "CONFLICT"
Check-Equal "H4c details.reason = idempotency_key_reused" ([string]$r.Json.error.details.reason) "idempotency_key_reused"

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body $idemBody -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = "bad key!" }
Check-Equal "H5 invalid key characters -> 400" $r.Status 400
Check-Equal "H5b details.reason = idempotency_key_invalid_chars" ([string]$r.Json.error.details.reason) "idempotency_key_invalid_chars"

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body $idemBody -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = ("k" * 129) }
Check-Equal "H6 over-long key -> 400" $r.Status 400

# The response must be remembered too: otherwise a client retry silently
# duplicates both the persisted user message and the AI call behind it
# (M3: 每次重试都是真金白银的模型调用)。Note the replayed body now carries the
# assistant message as well -- replaying a cheaper body would violate
# docs/02 section 7 ("the first response is replayed, including its status").
$sendKey = "stage2-idem-" + $stamp + "-b"
$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":"idem send"}' -Token $tokenA
$convIdem = [string]$r.Json.id
$r1 = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Body '{"content":"idempotent question"}' -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = $sendKey }
Check-Equal "H7 send with key -> 200" $r1.Status $script:SendStatus
$r2 = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Body '{"content":"idempotent question"}' -Token $tokenA -ExtraHeaders @{ 'Idempotency-Key' = $sendKey }
Check-Equal "H8 retry of the send -> replayed 200" $r2.Status $script:SendStatus
Check-Equal "H8b replay marked" (Get-Header $r2.Headers 'Idempotency-Replayed') "true"
Check-JsonEqual "H8c replay returns the same JSON value (assistant included)" $r1.Body $r2.Body
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Token $tokenA
Check-Equal "H9 the retry did not duplicate the round" (Item-Count $r.Json.items) $script:MsgsPerSend

$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Body '{"content":"no key here"}' -Token $tokenA
Check-Equal "H10 without a key sends are not deduplicated -> 200" $r.Status $script:SendStatus
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Body '{"content":"no key here"}' -Token $tokenA
Check-Equal "H10b second one also 200" $r.Status $script:SendStatus
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convIdem + "/messages") -Token $tokenA
Check-Equal "H10c all three rounds landed (6 messages total)" (Item-Count $r.Json.items) (3 * $script:MsgsPerSend)

# -----------------------------------------------------------------------------
# I. soft delete cascade  (AC-CONV-10)
# -----------------------------------------------------------------------------
Write-Section "I. soft delete (AC-CONV-10)"

$r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body '{"title":"to be deleted"}' -Token $tokenA
$convDel = [string]$r.Json.id
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convDel + "/messages") -Body '{"content":"will be orphaned"}' -Token $tokenA
Check-Equal "I1 seed one round -> 200" $r.Status $script:SendStatus

$r = Invoke-Api -Method DELETE -Path ("/api/v1/conversations/" + $convDel) -Token $tokenA
Check-Equal "I2 DELETE conversation -> 204" $r.Status 204
Check-Equal "I2b body is empty" $r.Body ""

$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convDel) -Token $tokenA
Check-Equal "I3 soft-deleted conversation -> 404" $r.Status 404
$r = Invoke-Api -Method GET -Path ("/api/v1/conversations/" + $convDel + "/messages") -Token $tokenA
Check-Equal "I4 messages of a soft-deleted conversation -> 404 too" $r.Status 404
Check-Equal "I4b code = CONVERSATION_NOT_FOUND" (Error-Code $r) "CONVERSATION_NOT_FOUND"
$r = Invoke-Api -Method DELETE -Path ("/api/v1/conversations/" + $convDel) -Token $tokenA
Check-Equal "I5 deleting again -> 404 (404 over 204, docs/02 section 7)" $r.Status 404
$r = Invoke-Api -Method POST -Path ("/api/v1/conversations/" + $convDel + "/messages") -Body '{"content":"into the void"}' -Token $tokenA
Check-Equal "I6 sending to a deleted conversation -> 404" $r.Status 404

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=100" -Token $tokenA
$stillListed = $false
foreach ($c in @($r.Json.items)) { if ($c.id -eq $convDel) { $stillListed = $true } }
Check "I7 deleted conversation is not listed" (-not $stillListed) (Truncate-Body $r.Body)

# -----------------------------------------------------------------------------
# J. cursor pagination
# -----------------------------------------------------------------------------
Write-Section "J. cursor pagination (REQ-AUTH-010)"

# Start from a clean slate so "exactly N" assertions mean something.
$cursor = ""
do {
    $page = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=100" -Token $tokenA
    foreach ($c in @($page.Json.items)) {
        Invoke-Api -Method DELETE -Path ("/api/v1/conversations/" + $c.id) -Token $tokenA | Out-Null
    }
    if ([string]$page.Json.has_more -eq "True") { $cursor = [string]$page.Json.next_cursor } else { $cursor = "" }
} while ($cursor)

$createdIDs = @()
foreach ($name in @("page-alpha", "page-bravo", "page-charlie", "page-delta", "page-echo")) {
    $r = Invoke-Api -Method POST -Path "/api/v1/conversations" -Body ('{"title":"' + $name + '"}') -Token $tokenA
    if ($r.Status -eq 201) { $createdIDs += [string]$r.Json.id }
}
Check-Equal "J0 five conversations created" $createdIDs.Count 5

$p1 = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=2" -Token $tokenA
Check-Equal "J1 page 1 -> 200" $p1.Status 200
Check-Equal "J1b page 1 has 2 items" (Item-Count $p1.Json.items) 2
Check-Equal "J1c has_more = true" ([string]$p1.Json.has_more) "True"
$cur1 = [string]$p1.Json.next_cursor
Check "J1d next_cursor present" ($cur1.Length -gt 0) ("cursor=" + $cur1)

$p2 = Invoke-Api -Method GET -Path ("/api/v1/conversations?limit=2&cursor=" + [uri]::EscapeDataString($cur1)) -Token $tokenA
Check-Equal "J2 page 2 -> 200" $p2.Status 200
Check-Equal "J2b page 2 has 2 items" (Item-Count $p2.Json.items) 2
Check-Equal "J2c has_more = true" ([string]$p2.Json.has_more) "True"
$cur2 = [string]$p2.Json.next_cursor
Check "J2d cursor advanced" ($cur2 -ne $cur1) ("c1=" + $cur1 + " c2=" + $cur2)

$p3 = Invoke-Api -Method GET -Path ("/api/v1/conversations?limit=2&cursor=" + [uri]::EscapeDataString($cur2)) -Token $tokenA
Check-Equal "J3 page 3 has the last item" (Item-Count $p3.Json.items) 1
Check-Equal "J3b has_more = false" ([string]$p3.Json.has_more) "False"
Check "J3c next_cursor is null on the last page" ($null -eq $p3.Json.next_cursor) (Truncate-Body $p3.Body)

$seen = @()
foreach ($pg in @($p1, $p2, $p3)) {
    foreach ($c in @($pg.Json.items)) { $seen += [string]$c.id }
}
Check-Equal "J4 three pages cover 5 items" $seen.Count 5
Check-Equal "J5 no id repeats across pages" (($seen | Sort-Object -Unique).Count) 5
$missing = @()
foreach ($id in $createdIDs) { if ($seen -notcontains $id) { $missing += $id } }
Check-Equal "J6 every created conversation appeared exactly once" $missing.Count 0

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?cursor=not-a-cursor" -Token $tokenA
# A malformed cursor is a client mistake: it must be a 400, not a 500. Both
# pagination cursors live in data and already answer with errs.CodeInvalidArgument,
# and biz.wrapDB preserves an existing *errs.AppError instead of overwriting it.
Check-Equal "J7 malformed cursor -> 400 (not 500)" $r.Status 400
Check-Equal "J7b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=101" -Token $tokenA
Check-Equal "J8 limit=101 -> 400 INVALID_ARGUMENT" $r.Status 400
Check-Equal "J8b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"
Check-Equal "J8c field = limit" ([string](Field-Error $r).field) "limit"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?limit=0" -Token $tokenA
Check-Equal "J9 limit=0 -> 400 (not treated as 'use the default')" $r.Status 400

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?status=deleted" -Token $tokenA
Check-Equal "J10 unknown status -> 400" $r.Status 400
Check-Equal "J10b field = status" ([string](Field-Error $r).field) "status"

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?pinned=true&limit=100" -Token $tokenA
Check-Equal "J11 pinned=true filter -> 200" $r.Status 200
Check-Equal "J11b nothing is pinned" (Item-Count $r.Json.items) 0

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?pinned=maybe" -Token $tokenA
Check-Equal "J12 pinned=maybe -> 400" $r.Status 400

$r = Invoke-Api -Method GET -Path "/api/v1/conversations?keyword=alpha&limit=100" -Token $tokenA
Check-Equal "J13 keyword filter -> 200" $r.Status 200
Check-Equal "J13b exactly one match" (Item-Count $r.Json.items) 1
Check-Equal "J13c matched the right one" ([string]$r.Json.items[0].title) "page-alpha"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host (" PASSED: " + $script:Passed + "   FAILED: " + $script:Failed)
Write-Host "=============================================" -ForegroundColor Cyan

if ($script:Failed -gt 0) { exit 1 }
exit 0
