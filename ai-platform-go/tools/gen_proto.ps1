# Regenerate the Go stubs for proto/aiplatform/v1/*.proto.
#
# ASCII only: PowerShell 5.1 parses BOM-less files as GBK, so any non-ASCII text
# here would break the parser.
#
# Prerequisites (all present on the dev machine, all on PATH):
#   protoc              (C:\protobuf\bin\protoc.exe, v27.3)
#   protoc-gen-go       (v1.36.10)
#   protoc-gen-go-grpc  (v1.5.1)
#
# Why a script instead of documenting the command: the output path is decided by
# `--go_opt=module=<module>`, which must match the `go_package` in the .proto
# exactly. Getting it wrong silently writes the stubs to a nested
# `github.com/...` directory that still compiles but is imported from nowhere.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\gen_proto.ps1

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$protoDir = Join-Path $root 'proto'
$module = 'github.com/YunfeiSHU/ai-assistant/ai-platform-go'

# The Go import path of a generated file is (go_package) with the module prefix
# stripped off, so the stubs land in `api/aiplatform/v1/`. Read it back from the
# .proto rather than hardcoding it twice.
$protoString = Get-Content (Join-Path $protoDir 'aiplatform\v1\chat.proto') -Raw -Encoding UTF8
if ($protoString -notmatch 'option\s+go_package\s*=\s*"([^"]+)"') {
    Write-Host 'FAIL: chat.proto has no option go_package' -ForegroundColor Red
    exit 1
}
$goPackage = $Matches[1]
$wantPrefix = $module + '/'
if (-not $goPackage.StartsWith($wantPrefix)) {
    Write-Host ("FAIL: go_package " + $goPackage + " does not start with " + $wantPrefix) -ForegroundColor Red
    exit 1
}
# go_package is "import/path;pkgname" -- split on ';' FIRST. Splitting on '/;'
# instead leaves the alias glued to the last path element and mkdir happily
# creates a directory literally named "v1;aiplatformv1".
$goImportPath = ($goPackage -split ';')[0]
$outRel = $goImportPath.Substring($wantPrefix.Length) -replace '\.', '\'
$outDir = Join-Path $root $outRel
Write-Host ("module   = " + $module)
Write-Host ("go_pkg   = " + $goPackage)
Write-Host ("out_dir  = " + $outDir)

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Work from $root with RELATIVE protoc arguments only.
#
# The repo path contains non-ASCII characters (作品集). PowerShell 5.1 encodes
# arguments handed to native executables with the ANSI code page, so an absolute
# --proto_path arrives at protoc as mojibake and it reports
# "directory does not exist" -- pointing at a directory that plainly exists.
# Relative paths keep every argument ASCII, which sidesteps the whole issue.
Set-Location $root
& protoc `
    --proto_path=proto `
    --go_out=. "--go_opt=module=$module" `
    --go-grpc_out=. "--go-grpc_opt=module=$module" `
    "aiplatform/v1/chat.proto"
if ($LASTEXITCODE -ne 0) {
    Write-Host ("FAIL: protoc exit " + $LASTEXITCODE) -ForegroundColor Red
    exit 1
}

# Self-check: an empty or missing file here means the flags above were wrong,
# and "compiles but nobody imports it" is the exact failure this guards against.
$generated = @(Get-ChildItem -Path $outDir -Filter '*.go' | Select-Object -ExpandProperty Name)
if ($generated.Count -eq 0) {
    Write-Host 'FAIL: protoc reported success but generated no .go files' -ForegroundColor Red
    exit 1
}
Write-Host ("OK: generated " + ($generated -join ', ')) -ForegroundColor Green
exit 0
