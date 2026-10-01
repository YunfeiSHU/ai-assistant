# =============================================================================
# M1 stage acceptance: health / auth / me  (ASCII only -- PS 5.1 parses BOM-less
# files as GBK, so non-ASCII text here would break the parser)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage1.ps1
#   powershell -ExecutionPolicy Bypass -File tools\curl_stage1.ps1 -BaseUrl http://127.0.0.1:18080
#
# Default port is 18080, NOT 8080. On this machine port 8080 is taken by an
# invisible Spring service (docs/08 section 7.2). Hitting it does not fail in a
# recognisable way: it answers 404 with a Spring error envelope, so the whole
# run reports ~87 failures that have nothing to do with the gateway.
#
# Preconditions:
#   1. MySQL ai_platform has the gateway tables (deploy/mysql/001_gateway_tables.sql)
#   2. test accounts exist (deploy/mysql/002_test_accounts.sql)
#   3. gateway is running with ENV_FILE pointing at ai-platform-go\.env
# =============================================================================

param(
    [string]$BaseUrl = "http://127.0.0.1:18080",
    [string]$Stage1Email = "stage1@test.local",
    [string]$Stage1Password = "Stage1#Test2026",
    [string]$DisabledEmail = "disabled@test.local",
    [string]$DisabledPassword = "Disabled#Test2026"
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:Passed = 0
$script:Failed = 0

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

# ---- Kill the harness's own floor: turn the system proxy off ----
#
# With a local proxy resident (Clash etc. at 127.0.0.1:7897), PS 5.1's
# `Invoke-WebRequest` sends even **loopback** addresses through proxy resolution.
# Measured on this machine: the same `/health` -> IWR times out after 10s while
# `curl.exe --noproxy '*'` answers in 412ms (run_all_stages.ps1 header, docs/09
# section 2.1). Every request in this script targets 127.0.0.1, so disabling the
# proxy is pure profit -- it removes a **per-round-trip** fixed cost (M2 alone
# makes 96 `Invoke-Api` calls; M1/M3/M4/M5/M6 make tens each).
#
# Why the .NET static instead of only `-Proxy $null`: PS 5.1's `-Proxy` ends up in
# `if (Proxy != null) { request.Proxy = Proxy }`, so passing `$null` is equivalent
# to **not passing it** on some builds (the proxy stays active). Setting the
# process-wide default is the part that deterministically takes effect; Invoke-Api
# also carries `-Proxy $null` (or `-NoProxy` on PS 6+) as a second line of defence.
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
        Method           = $Method
        Uri              = ($BaseUrl + $Path)
        Headers          = $reqHeaders
        UseBasicParsing  = $true
        TimeoutSec       = 30
    }
    if ($null -ne $Body) {
        if ($Body -is [string]) {
            $payload = $Body
        } else {
            $payload = ($Body | ConvertTo-Json -Depth 8 -Compress)
        }
        # Encode explicitly: PS 5.1 would otherwise send the body as ISO-8859-1
        # and mangle any non-ASCII character.
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
        # PowerShell 5.1 does not have -SkipHttpErrorCheck (PS 7+ only), and the
        # response stream on the WebException is already consumed by the
        # pipeline. $_.ErrorDetails.Message is the supported way to get the body.
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
function Has-Header($headers, [string]$name) {
    if ($null -eq $headers) { return $false }
    if ($headers -is [System.Collections.IDictionary]) { return $headers.ContainsKey($name) }
    foreach ($k in $headers.AllKeys) { if ($k -ieq $name) { return $true } }
    return $false
}

function Get-Header($headers, [string]$name) {
    if ($null -eq $headers) { return "" }
    if ($headers -is [System.Collections.IDictionary]) {
        if ($headers.ContainsKey($name)) { return [string]$headers[$name] }
        return ""
    }
    foreach ($k in $headers.AllKeys) { if ($k -ieq $name) { return [string]$headers[$k] } }
    return ""
}

function Header-Names($headers) {
    if ($null -eq $headers) { return "" }
    if ($headers -is [System.Collections.IDictionary]) { return ($headers.Keys -join ",") }
    return ($headers.AllKeys -join ",")
}

function Error-Code($resp) {
    if ($resp.Json -and $resp.Json.error -and $resp.Json.error.code) { return [string]$resp.Json.error.code }
    return ""
}

function Truncate-Body([string]$s, [int]$max = 260) {
    if ($null -eq $s) { return "" }
    $s = $s -replace "\s+", " "
    if ($s.Length -le $max) { return $s }
    return $s.Substring(0, $max) + "..."
}

# Invoke-WebRequest returns a System.Net.WebHeaderCollection, which has neither
# ContainsKey nor Keys -- so header access goes through these two helpers.
function Has-Header($headers, [string]$name) {
    if ($null -eq $headers) { return $false }
    if ($headers -is [System.Collections.IDictionary]) { return $headers.ContainsKey($name) }
    foreach ($k in $headers.AllKeys) { if ($k -ieq $name) { return $true } }
    return $false
}

function Get-Header($headers, [string]$name) {
    if ($null -eq $headers) { return "" }
    if ($headers -is [System.Collections.IDictionary]) {
        if ($headers.ContainsKey($name)) { return [string]$headers[$name] }
        return ""
    }
    foreach ($k in $headers.AllKeys) { if ($k -ieq $name) { return [string]$headers[$k] } }
    return ""
}

function Header-Names($headers) {
    if ($null -eq $headers) { return "" }
    if ($headers -is [System.Collections.IDictionary]) { return ($headers.Keys -join ",") }
    return ($headers.AllKeys -join ",")
}

# -----------------------------------------------------------------------------
# A. health
# -----------------------------------------------------------------------------
Write-Section "A. health"

$r = Invoke-Api -Method GET -Path "/health/live"
Check-Equal "A1 GET /health/live -> 200" $r.Status 200
Check-Equal "A1b status field = ok" ([string]$r.Json.status) "ok"
Check "A1c request id header echoed" (Has-Header $r.Headers 'X-Request-Id') ("headers=" + (Header-Names $r.Headers))
Check "A1d trace id header present" (Has-Header $r.Headers 'X-Trace-Id') "missing X-Trace-Id"

$r = Invoke-Api -Method GET -Path "/health/ready"
Check-Equal "A2 GET /health/ready -> 200" $r.Status 200
Check-Equal "A2b mysql check ok" ([string]$r.Json.checks.mysql.ok) "True"
Check-Equal "A2c redis check ok" ([string]$r.Json.checks.redis.ok) "True"
Check-Equal "A2d schema_version present" ([string]$r.Json.checks.mysql.schema_version) "20260929_01"

$r = Invoke-Api -Method GET -Path "/health"
Check-Equal "A3 GET /health -> 200 always" $r.Status 200
Check "A3b uptime_seconds is a number" ($null -ne $r.Json.uptime_seconds) (Truncate-Body $r.Body)
Check-Equal "A3c api/v1 alias works" (Invoke-Api -Method GET -Path "/api/v1/health/live").Status 200

# W3C traceparent must be honoured when supplied by an upstream gateway.
$tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
$r = Invoke-Api -Method GET -Path "/health/live" -ExtraHeaders @{ 'traceparent' = $tp }
Check-Equal "A4 inbound traceparent is reused" (Get-Header $r.Headers 'X-Trace-Id') "4bf92f3577b34da6a3ce929d0e0e4736"

# -----------------------------------------------------------------------------
# B. register
# -----------------------------------------------------------------------------
Write-Section "B. register"

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
$regEmail = "m1_reg_$stamp@test.local"
$regPassword = "M1Reg#2026pass"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{
    email = $regEmail; password = $regPassword; nickname = "M1 register probe"
}
Check-Equal "B1 POST /auth/register -> 201" $r.Status 201
Check "B1b response has id" ([string]$r.Json.id).StartsWith("u_") ("id=" + [string]$r.Json.id)
Check-Equal "B1c email normalised to lower case" ([string]$r.Json.email) $regEmail
Check-Equal "B1d plan defaults to free" ([string]$r.Json.plan) "free"
Check-Equal "B1e status active" ([string]$r.Json.status) "active"

$leaked = $r.Body
Check "B1f no password/hash field leaks" (-not ($leaked -match '(?i)(password|hash|token_version|salt)')) (Truncate-Body $leaked)

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{
    email = $regEmail; password = $regPassword
}
Check-Equal "B2 duplicate email -> 409" $r.Status 409
Check-Equal "B2b code = EMAIL_ALREADY_EXISTS" (Error-Code $r) "EMAIL_ALREADY_EXISTS"
Check "B2c error body has trace_id" ([string]$r.Json.error.trace_id).Length -gt 0 (Truncate-Body $r.Body)

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{ email = "m1_weak_$stamp@test.local"; password = "12345678" }
Check-Equal "B3 weak password -> 400" $r.Status 400
# docs/02 section 4.1 has no dedicated weak-password code: every field-level
# validation failure is INVALID_ARGUMENT with details.fields[].
Check-Equal "B3b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"
Check-Equal "B3c details.fields[0].field = password" ([string]$r.Json.error.details.fields[0].field) "password"
Check "B3d reason is too_common or too_short" `
    (([string]$r.Json.error.details.fields[0].reason) -in @("too_common", "too_short")) `
    ("reason=" + [string]$r.Json.error.details.fields[0].reason)

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body @{ email = "not-an-email"; password = $regPassword }
Check-Equal "B4 malformed email -> 400" $r.Status 400
Check-Equal "B4b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body '{"email":'
Check-Equal "B5 truncated json -> 400" $r.Status 400
Check-Equal "B5b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"
Check "B5c reason = truncated_json (not empty_body)" `
    (([string]$r.Json.error.details.reason) -eq "truncated_json") `
    ("body=" + (Truncate-Body $r.Body))

# Complete but syntactically invalid JSON: must be malformed_json, a third
# distinct reason from truncated_json and empty_body.
$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body '{email: 1}'
Check-Equal "B5d malformed json -> 400" $r.Status 400
Check "B5e reason = malformed_json" `
    (([string]$r.Json.error.details.reason) -eq "malformed_json") `
    ("body=" + (Truncate-Body $r.Body))

$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body "" -ContentType "text/plain"
Check-Equal "B6 empty body -> 400" $r.Status 400
Check "B6b reason = empty_body" `
    (([string]$r.Json.error.details.reason) -eq "empty_body") `
    ("body=" + (Truncate-Body $r.Body))

# Body limit is enforced before parsing, so a 2MB body must be 413 and must NOT
# be parsed at all (a parsed-then-rejected body still costs CPU and memory).
$big = '{"email":"' + ("a" * 2000000) + '"}'
$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" -Body $big
Check-Equal "B7 oversized body -> 413" $r.Status 413
Check-Equal "B7b code = PAYLOAD_TOO_LARGE" (Error-Code $r) "PAYLOAD_TOO_LARGE"

# docs/02 defines UNSUPPORTED_MEDIA_TYPE, but only for uploads (section 4.2).
# JSON endpoints deliberately do not enforce a media-type check: curling without
# -H is a first-class use case, and rejecting a perfectly parseable body with 415
# costs more than it protects. Asserted here so the behaviour is intentional.
$ctypeEmail = "m1_ctype_$stamp@test.local"
$r = Invoke-Api -Method POST -Path "/api/v1/auth/register" `
    -Body ('{"email":"' + $ctypeEmail + '","password":"M1Ctype#2026pass"}') `
    -ContentType "application/x-www-form-urlencoded"
Check-Equal "B8 non-JSON Content-Type still bound as JSON" $r.Status 201

# -----------------------------------------------------------------------------
# C. login
# -----------------------------------------------------------------------------
Write-Section "C. login"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{
    email = $Stage1Email; password = $Stage1Password; device_name = "stage1-curl"
}
Check-Equal "C1 POST /auth/login -> 200" $r.Status 200
$access1 = [string]$r.Json.access_token
$refresh1 = [string]$r.Json.refresh_token
Check "C1b access_token present" ($access1.Length -gt 20) "len=" + $access1.Length
Check "C1c refresh_token present" ($refresh1.Length -gt 20) "len=" + $refresh1.Length
Check-Equal "C1d token_type = Bearer" ([string]$r.Json.token_type) "Bearer"
Check "C1e expires_in > 0" ([int]$r.Json.expires_in -gt 0) ("expires_in=" + [string]$r.Json.expires_in)

# The access token must carry the JWT contract from docs/02 section 3.2 (J1).
$parts = $access1.Split(".")
Check-Equal "C2 JWT has 3 segments" $parts.Count 3
if ($parts.Count -eq 3) {
    $padded = $parts[1].Replace("-", "+").Replace("_", "/")
    while ($padded.Length % 4 -ne 0) { $padded += "=" }
    $claims = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($padded)) | ConvertFrom-Json
    Check-Equal "C2b iss = ai-assistant" ([string]$claims.iss) "ai-assistant"
    Check-Equal "C2c aud = ai-platform" ([string]$claims.aud) "ai-platform"
    Check-Equal "C2d token_type = access" ([string]$claims.token_type) "access"
    Check "C2e sub starts with u_" ([string]$claims.sub).StartsWith("u_") ("sub=" + [string]$claims.sub)
    Check "C2f ver is present" ($null -ne $claims.ver) "missing ver"
}

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $Stage1Email; password = "Wrong#Password1" }
Check-Equal "C3 wrong password -> 401" $r.Status 401
Check-Equal "C3b code = INVALID_CREDENTIALS" (Error-Code $r) "INVALID_CREDENTIALS"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = "nobody_$stamp@test.local"; password = "Wrong#Password1" }
Check-Equal "C4 unknown email -> 401 (not 404)" $r.Status 401
# Anti-enumeration: unknown email and wrong password MUST be indistinguishable.
Check-Equal "C4b same code as wrong password" (Error-Code $r) "INVALID_CREDENTIALS"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $DisabledEmail; password = $DisabledPassword }
Check-Equal "C5 disabled account -> 403" $r.Status 403
Check-Equal "C5b code = USER_DISABLED" (Error-Code $r) "USER_DISABLED"

# -----------------------------------------------------------------------------
# D. me
# -----------------------------------------------------------------------------
Write-Section "D. me"

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $access1
Check-Equal "D1 GET /me -> 200" $r.Status 200
Check-Equal "D1b email matches" ([string]$r.Json.email) $Stage1Email
Check "D1c no secret fields" (-not ($r.Body -match '(?i)(password|hash|token_version|salt)')) (Truncate-Body $r.Body)

$r = Invoke-Api -Method GET -Path "/api/v1/me"
Check-Equal "D2 missing Authorization -> 401" $r.Status 401
Check-Equal "D2b code = UNAUTHENTICATED" (Error-Code $r) "UNAUTHENTICATED"

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token "not.a.jwt"
Check-Equal "D3 malformed token -> 401" $r.Status 401

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token ($access1 + "x")
Check-Equal "D4 tampered signature -> 401" $r.Status 401

$r = Invoke-Api -Method GET -Path "/api/v1/me" -ExtraHeaders @{ Authorization = $access1 }
Check-Equal "D5 missing Bearer scheme -> 401" $r.Status 401

$r = Invoke-Api -Method PATCH -Path "/api/v1/me" -Token $access1 -Body @{ nickname = "M1 renamed" }
Check-Equal "D6 PATCH /me -> 200" $r.Status 200
Check-Equal "D6b nickname updated" ([string]$r.Json.nickname) "M1 renamed"

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $access1
Check-Equal "D6c nickname persisted" ([string]$r.Json.nickname) "M1 renamed"

$r = Invoke-Api -Method PATCH -Path "/api/v1/me" -Token $access1 -Body @{ nickname = ("x" * 100) }
Check-Equal "D7 too long nickname -> 400" $r.Status 400

# -----------------------------------------------------------------------------
# E. refresh rotation
# -----------------------------------------------------------------------------
Write-Section "E. refresh rotation"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refresh1 }
Check-Equal "E1 POST /auth/refresh -> 200" $r.Status 200
$access2 = [string]$r.Json.access_token
$refresh2 = [string]$r.Json.refresh_token
Check "E1b new refresh token differs" ($refresh2 -ne $refresh1) "refresh token was not rotated"
# Do NOT assert a new access token differs from the old one: the JWT contract
# (docs/02 section 3.2) has no `jti`, and exp/iat are second-granularity, so two
# tokens minted in the same second are byte-identical BY DESIGN. The meaningful
# contract assertions are (a) both work and (b) refreshing does not revoke the
# old access token -- access tokens are only invalidated by `ver` (section 3.1).
$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $access1
Check-Equal "E1c old access token still valid after refresh" $r.Status 200

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $access2
Check-Equal "E2 rotated access token works" $r.Status 200

$r = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refresh1 }
Check-Equal "E3 reusing the old refresh token -> 401" $r.Status 401
Check-Equal "E3b code = INVALID_REFRESH_TOKEN" (Error-Code $r) "INVALID_REFRESH_TOKEN"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = "rt_" + ("a" * 43) }
Check-Equal "E4 unknown refresh token -> 401" $r.Status 401

# -----------------------------------------------------------------------------
# F. logout
# -----------------------------------------------------------------------------
Write-Section "F. logout"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/logout" -Token $access2
Check-Equal "F1 POST /auth/logout -> 204" $r.Status 204
Check-Equal "F1b no body on 204" $r.Body.Length 0

$r = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refresh2 }
Check-Equal "F2 refresh token revoked by logout -> 401" $r.Status 401

$r = Invoke-Api -Method POST -Path "/api/v1/auth/logout"
Check-Equal "F3 logout without token -> 401" $r.Status 401

# -----------------------------------------------------------------------------
# G. change password
# -----------------------------------------------------------------------------
Write-Section "G. change password"

# Uses the throwaway account created in section B so the shared fixture account
# keeps its known password across runs.
$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $regEmail; password = $regPassword }
Check-Equal "G0 login with the registered password -> 200" $r.Status 200
$accA = [string]$r.Json.access_token
$refA = [string]$r.Json.refresh_token

$r = Invoke-Api -Method POST -Path "/api/v1/auth/password" -Token $accA -Body @{
    old_password = $regPassword; new_password = "M1Reg#2026newpass"
}
Check-Equal "G1 POST /auth/password -> 204" $r.Status 204

$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $accA
Check-Equal "G2 old access token invalidated (ver bumped) -> 401" $r.Status 401
Check-Equal "G2b code = UNAUTHENTICATED (not TOKEN_EXPIRED)" (Error-Code $r) "UNAUTHENTICATED"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/refresh" -Body @{ refresh_token = $refA }
Check-Equal "G3 all refresh tokens revoked -> 401" $r.Status 401

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $regEmail; password = $regPassword }
Check-Equal "G4 old password rejected -> 401" $r.Status 401

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $regEmail; password = "M1Reg#2026newpass" }
Check-Equal "G5 new password accepted -> 200" $r.Status 200

$r = Invoke-Api -Method POST -Path "/api/v1/auth/password" -Token ([string]$r.Json.access_token) -Body @{
    old_password = "Wrong#Current1"; new_password = "M1Reg#2026third"
}
Check-Equal "G6 wrong old password -> 401" $r.Status 401
Check-Equal "G6b code = INVALID_CREDENTIALS" (Error-Code $r) "INVALID_CREDENTIALS"

$r = Invoke-Api -Method POST -Path "/api/v1/auth/password" -Token $access1 -Body @{
    old_password = $Stage1Password; new_password = "short"
}
Check-Equal "G7 too short new password -> 400" $r.Status 400
Check-Equal "G7b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"
Check-Equal "G7c details.fields[0].field = new_password" ([string]$r.Json.error.details.fields[0].field) "new_password"

# logout_all must bump token_version so the access token dies immediately.
$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body @{ email = $Stage1Email; password = $Stage1Password }
$accB = [string]$r.Json.access_token
$r = Invoke-Api -Method POST -Path "/api/v1/auth/logout" -Token $accB -Body @{ logout_all = $true }
Check-Equal "G8 logout_all -> 204" $r.Status 204
$r = Invoke-Api -Method GET -Path "/api/v1/me" -Token $accB
Check-Equal "G9 logout_all invalidates access immediately -> 401" $r.Status 401

# -----------------------------------------------------------------------------
# H. error contract
# -----------------------------------------------------------------------------
Write-Section "H. error contract"

$r = Invoke-Api -Method GET -Path "/api/v1/does-not-exist"
Check-Equal "H1 unknown path -> 404" $r.Status 404
Check-Equal "H1b code = RESOURCE_NOT_FOUND" (Error-Code $r) "RESOURCE_NOT_FOUND"
Check "H1c envelope shape {error:{code,message,trace_id}}" `
    (($null -ne $r.Json.error) -and ($null -ne $r.Json.error.message) -and ($null -ne $r.Json.error.trace_id)) `
    (Truncate-Body $r.Body)
Check "H1d trace_id matches X-Trace-Id header" `
    ([string]$r.Json.error.trace_id -eq (Get-Header $r.Headers 'X-Trace-Id')) `
    ("body=" + [string]$r.Json.error.trace_id + " header=" + (Get-Header $r.Headers 'X-Trace-Id'))

$r = Invoke-Api -Method GET -Path "/api/v1/me`?x=1" -Token $access1
Check "H2 query string does not break auth" ($r.Status -eq 401 -or $r.Status -eq 200) ("status=" + $r.Status)

$r = Invoke-Api -Method POST -Path "/api/v1/auth/login" -Body ('{"email":"' + $Stage1Email + '"}')
Check-Equal "H3 missing password field -> 400" $r.Status 400
Check-Equal "H3b code = INVALID_ARGUMENT" (Error-Code $r) "INVALID_ARGUMENT"

# Every 4xx/5xx body must carry the full envelope; a partial envelope breaks
# client error handling far more subtly than a wrong status code would.
$envelopeOk = $true
$envelopeDetail = ""
foreach ($probe in @(
    @{ m = "GET";  p = "/api/v1/does-not-exist"; t = $null },
    @{ m = "GET";  p = "/api/v1/me";             t = $null },
    @{ m = "POST"; p = "/api/v1/auth/refresh";   t = $null }
)) {
    $rr = Invoke-Api -Method $probe.m -Path $probe.p -Token $probe.t
    $e = $rr.Json.error
    if (($null -eq $e) -or (-not $e.code) -or (-not $e.message) -or (-not $e.trace_id) -or ($null -eq $e.retryable)) {
        $envelopeOk = $false
        $envelopeDetail = $probe.p + " -> " + (Truncate-Body $rr.Body)
    }
}
Check "H4 all error bodies carry code/message/trace_id/retryable" $envelopeOk $envelopeDetail

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host (" PASSED: " + $script:Passed + "   FAILED: " + $script:Failed)
Write-Host "=============================================" -ForegroundColor Cyan

if ($script:Failed -gt 0) { exit 1 }
exit 0
