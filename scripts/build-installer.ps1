[CmdletBinding()]
param(
    [string]$Iscc = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "release\DiscDock\DiscDock.exe"))) {
    throw "Build the release first with scripts\build-release.ps1."
}

if (-not $Iscc) {
    $onPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
    $candidates = @(
        $(if ($onPath) { $onPath.Source }),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    $Iscc = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $Iscc) {
    throw "Inno Setup 6 is required. Install it with: winget install JRSoftware.InnoSetup"
}

$versionMatch = Select-String -LiteralPath (Join-Path $projectRoot "service\discdock\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw "Could not read the DiscDock version from service\discdock\__init__.py." }
$version = $versionMatch.Matches[0].Groups[1].Value

& $Iscc "/DAppVersion=$version" (Join-Path $projectRoot "installer\DiscDock.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the installer." }

$setup = Join-Path $projectRoot "release\installer\DiscDock-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $setup)) { throw "The installer was not created at $setup." }
Write-Host "Installer created at $setup" -ForegroundColor Green
