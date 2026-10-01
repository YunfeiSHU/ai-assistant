param(
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Test-Falsify {
    param([string]$Path, [string]$From, [string]$To, [string]$TestPattern, [string]$Pkg)

    $full = Join-Path $ProjectRoot $Path
    $bak = [IO.File]::ReadAllText($full, [Text.UTF8Encoding]::new($false))
    if (-not $bak.Contains($From)) {
        Write-Host "SKIP (pattern not found): $TestPattern"
        return
    }
    $patched = $bak.Replace($From, $To)
    [IO.File]::WriteAllText($full, $patched, [Text.UTF8Encoding]::new($false))

    Push-Location $ProjectRoot
    $out = & go test $Pkg -run $TestPattern -count=1 2>&1 | Out-String
    Pop-Location

    [IO.File]::WriteAllText($full, $bak, [Text.UTF8Encoding]::new($false))

    if ($out -match 'FAIL') {
        Write-Host ("FALSIFIED-OK  " + $TestPattern)
    }
    else {
        Write-Host ("NOT-FALSIFIED " + $TestPattern + "  <-- 测试没有在守护这条护栏")
    }
}

Test-Falsify 'internal\biz\message_stream.go' 'resetTimer(idle, s.d.StreamIdleTimeout)' '_ = idle' 'TestStreamSendIdleTimerIsResetByEachEvent' './internal/biz/'
Test-Falsify 'internal\biz\message_stream.go' 'context.WithoutCancel(ctx)' 'ctx' 'TestStreamSendClientGoneStopsFramesButStillPersistsPartial' './internal/biz/'
Test-Falsify 'internal\biz\message_stream.go' 'if !acc.hasContent() {' 'if false {' 'TestStreamSendZeroEventFailureLeavesNoAssistantRow' './internal/biz/'
Test-Falsify 'internal\data\ai\http_stream.go' 'if d.dataSeen {' 'if d.data.Len() > 0 {' 'TestSSEDecoderLeadingEmptyDataLineIsPreserved' './internal/data/ai/'
Test-Falsify 'internal\biz\message_stream.go' 'a.references = out' 'a.references = append(a.references, out...)' 'TestStreamSendReplacesReferencesInsteadOfAppending' './internal/biz/'
Test-Falsify 'internal\biz\message_stream.go' 'n := utf8.RuneCountInString(delta)' 'n := len(delta)' 'TestStreamSendTruncationNeverSplitsARune' './internal/biz/'

exit 0
