# Regenerate the Python gRPC stubs for app/grpc/aiplatform/v1/*.proto.
#
# ASCII only: PowerShell 5.1 parses BOM-less files as GBK, so any non-ASCII text
# here would break the parser.
#
# ---------------------------------------------------------------------------
# The contract lives in the OTHER project: ../ai-platform-go/proto/aiplatform/v1
# That is deliberate -- the gateway owns the proto (docs/04 is the gateway's SRS
# and the AI side implements to it), so there is exactly one editable copy.
#
# ---------------------------------------------------------------------------
# Why the staging directory: protoc's Python plugin derives the generated
# MODULE PATH from the proto file's path *relative to --proto_path*, and it
# writes a correspondingly absolute import into the generated code:
#
#   proto at  aiplatform/v1/chat.proto   ->  import aiplatform.v1.chat_pb2
#   proto at  app/grpc/aiplatform/v1/chat.proto -> import app.grpc.aiplatform.v1.chat_pb2
#
# This project keeps 100% of its code inside the `app` package (see
# pyproject.toml: `packages = ["app"]`, ruff `known-first-party`, mypy
# `packages`). The first form would need `app/grpc` on sys.path at runtime --
# a real anti-pattern -- so the proto is staged at the second form instead.
# Net effect: correct absolute imports, no sys.path mutation, one editable proto.
#
# The staging root is under $env:TEMP so nothing is ever committed from it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\gen_grpc_stubs.ps1

$ErrorActionPreference = 'Continue'

$projectRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $projectRoot
$protoSrc = Join-Path $repoRoot 'ai-platform-go\proto\aiplatform\v1\chat.proto'
$outDir = Join-Path $projectRoot 'app\grpc\aiplatform\v1'

if (-not (Test-Path $protoSrc)) {
    Write-Host ("FAIL: contract not found at " + $protoSrc) -ForegroundColor Red
    exit 1
}

# ---- stage: <tmp>/app/grpc/aiplatform/v1/chat.proto ----
$stage = Join-Path $env:TEMP ('aigrpc-stage-' + [Guid]::NewGuid().ToString('N'))
$stageProtoDir = Join-Path $stage 'app\grpc\aiplatform\v1'
New-Item -ItemType Directory -Force -Path $stageProtoDir | Out-Null
Copy-Item $protoSrc (Join-Path $stageProtoDir 'chat.proto') -Force
Write-Host ("stage    = " + $stage)
Write-Host ("out_dir  = " + $outDir)

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# IMPORTANT: always write UTF-8 **without** BOM.
# `Set-Content -Encoding UTF8` writes a BOM on PowerShell 5.1. Python tolerates
# a BOM when importing, but `ast.parse()` on the raw text does NOT -- the repo's
# structural lint tests read source files with ast, so a BOM turns them into
# SyntaxError: invalid non-printable character U+FEFF.
function Write-Utf8NoBom([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, [Text.UTF8Encoding]::new($false))
}

# Keep the package chain importable. Two levels: aiplatform/ and aiplatform/v1/.
# 内容用**模块 docstring**而不是注释：注释规范（docs/15）要求"模块必须有 docstring"，
# 而这一行是生成物里唯一的说明位；tests/unit/test_comment_conventions.py 会检查它。
$stubDoc = '"""Generated package -- see tools/gen_grpc_stubs.ps1 (do not edit)."""'
Write-Utf8NoBom (Join-Path $projectRoot 'app\grpc\aiplatform\__init__.py') $stubDoc
foreach ($f in @('__init__.py')) {
    $p = Join-Path $outDir $f
    if (-not (Test-Path $p)) {
        Write-Utf8NoBom $p $stubDoc
    }
}

# Work from the project root with relative OUTPUT paths only: the repo path
# contains non-ASCII characters (作品集) and PowerShell 5.1 encodes arguments
# handed to native executables with the ANSI code page, so an absolute
# --python_out arrives mangled and protoc reports "no such file or directory".
#
# --proto_path is the ONE argument that may be absolute, because the staging
# root lives under $env:TEMP, which is pure ASCII.
#
# Net: the proto is read from the ASCII staging tree, the stubs are written to
# the non-ASCII project tree via ".". The imported module path is still derived
# from the proto's path relative to --proto_path, i.e. app.grpc.aiplatform.v1.
Set-Location $projectRoot
& "$projectRoot\.venv\Scripts\python.exe" -m grpc_tools.protoc `
    "--proto_path=$stage" `
    "--python_out=." `
    "--pyi_out=." `
    "--grpc_python_out=." `
    "app/grpc/aiplatform/v1/chat.proto"
$code = $LASTEXITCODE
Set-Location $projectRoot
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue

if ($code -ne 0) {
    Write-Host ("FAIL: protoc exit " + $code) -ForegroundColor Red
    exit 1
}

$generated = @(Get-ChildItem -Path $outDir -Filter '*.py' | Select-Object -ExpandProperty Name)
if ($generated.Count -lt 2) {
    Write-Host ("FAIL: expected at least 2 .py files, got " + ($generated -join ', ')) -ForegroundColor Red
    exit 1
}

# Self-check the import style: a bare `import aiplatform.v1...` here means the
# staging path was wrong and the stubs would only import with a sys.path hack.
$grpcStub = Get-Content (Join-Path $outDir 'chat_pb2_grpc.py') -Raw -Encoding UTF8
if ($grpcStub -notmatch 'app\.grpc\.aiplatform\.v1') {
    Write-Host 'FAIL: chat_pb2_grpc.py does not reference app.grpc.aiplatform.v1' -ForegroundColor Red
    Write-Host '      the stubs would need app/grpc on sys.path at runtime' -ForegroundColor Red
    exit 1
}

Write-Host ("OK: generated " + ($generated -join ', ')) -ForegroundColor Green
exit 0
