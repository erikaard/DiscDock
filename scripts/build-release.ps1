[CmdletBinding()]
param(
    [string]$NodeExe = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $NodeExe) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw "Node.js 22 or newer is required to build the dashboard." }
    $NodeExe = $nodeCommand.Source
}
$nodeVersion = & $NodeExe --version
if ([int](($nodeVersion -replace '^v','').Split('.')[0]) -lt 22) { throw "Node.js 22 or newer is required; found $nodeVersion." }

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        & ".\.venv\Scripts\python.exe" -m ruff check service
        if ($LASTEXITCODE -ne 0) { throw "Service lint failed." }
        Push-Location (Join-Path $projectRoot "service")
        try {
            # A prior elevated build can leave a fixed temp folder inaccessible
            # to a later normal-user build. Give every build its own test root.
            $testRoot = Join-Path $env:TEMP "discdock-release-tests-$PID"
            & "..\.venv\Scripts\python.exe" -m pytest -q -p no:cacheprovider --basetemp $testRoot
            if ($LASTEXITCODE -ne 0) { throw "Service tests failed." }
        } finally {
            Pop-Location
        }
        & $NodeExe "node_modules\typescript\bin\tsc" --noEmit --incremental false
        if ($LASTEXITCODE -ne 0) { throw "TypeScript checks failed." }
        & $NodeExe "node_modules\eslint\bin\eslint.js" app components hooks lib
        if ($LASTEXITCODE -ne 0) { throw "Dashboard lint failed." }
    }
    & $NodeExe "scripts\copy-ocr-assets.mjs"
    if ($LASTEXITCODE -ne 0) { throw "Copying the OCR files for the dashboard failed." }
    & $NodeExe "node_modules\next\dist\bin\next" build
    if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed." }
    $staticIndex = Join-Path $projectRoot "out\index.html"
    if (-not (Test-Path -LiteralPath $staticIndex)) {
        throw "Dashboard build did not produce a standalone index.html."
    }
    & ".\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath release --workpath build-release discdock.spec
    if ($LASTEXITCODE -ne 0) { throw "Windows packaging failed." }
    Write-Host "DiscDock release created at $projectRoot\release\DiscDock" -ForegroundColor Green
} finally {
    Pop-Location
}
