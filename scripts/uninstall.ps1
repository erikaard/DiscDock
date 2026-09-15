[CmdletBinding()]
param([switch]$RemoveProgramFiles)

$ErrorActionPreference = "Stop"
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\DiscDock"
Unregister-ScheduledTask -TaskName "DiscDock" -Confirm:$false -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "DiscDock" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\DiscDock.lnk") -Force -ErrorAction SilentlyContinue
Get-Process -Name "DiscDock" -ErrorAction SilentlyContinue | Stop-Process -Force
if ($RemoveProgramFiles -and (Test-Path -LiteralPath $installRoot)) {
    $resolved = (Resolve-Path -LiteralPath $installRoot).Path
    $expected = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\DiscDock"))
    if ($resolved -ne $expected) { throw "Refusing to remove an unexpected folder: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Host "DiscDock startup was removed. Your media and settings were preserved." -ForegroundColor Green
