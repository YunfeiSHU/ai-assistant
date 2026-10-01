# Repair go.mod: drop the whole indirect block, re-pin direct deps, let the build re-derive.
# ASCII only on purpose: create_file writes no BOM and PS 5.1 parses non-ASCII as GBK.
$ErrorActionPreference = 'Continue'
Set-Location 'c:\Users\79042\Desktop\作品集\ai-assistant\ai-platform-go'

$lines = Get-Content go.mod -Encoding UTF8
$mods = @()
foreach ($line in $lines) {
    if ($line -match '//\s*indirect') {
        if ($line -match '^\s*([^\s]+)\s+v') { $mods += $Matches[1] }
    }
}
"dropping $($mods.Count) indirect requirements"

$edit = @('mod', 'edit')
foreach ($m in $mods) { $edit += ('-droprequire=' + $m) }
& go @edit
"drop exit=$LASTEXITCODE"

& go mod edit '-go=1.24.0'
& go mod edit '-require=go.opentelemetry.io/otel@v1.31.0' '-require=go.opentelemetry.io/otel/trace@v1.31.0' '-require=go.opentelemetry.io/otel/sdk@v1.31.0' '-require=go.opentelemetry.io/otel/exporters/otlp/otlptrace@v1.31.0' '-require=go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc@v1.31.0' '-require=google.golang.org/grpc@v1.69.4' '-require=google.golang.org/protobuf@v1.36.9' '-require=golang.org/x/crypto@v0.45.0' '-require=github.com/prometheus/client_golang@v1.21.1'
"pin exit=$LASTEXITCODE"

& go mod edit '-droprerequire=go.opentelemetry.io/otel v1.43.0' 2>$null
"--- build (mod=mod) ---"
& go build -mod=mod ./... 2>&1 | Select-Object -First 25
"build exit=$LASTEXITCODE"
"--- go directive ---"
Select-String -Path go.mod -Pattern '^go ' | ForEach-Object { $_.Line }
exit 0
