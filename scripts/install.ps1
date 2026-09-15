[CmdletBinding()]
param(
    [switch]$InstallMediaTools,
    [switch]$NoStartup,
    [string]$ArmConfigPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$releaseRoot = Join-Path $projectRoot "release\DiscDock"
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\DiscDock"
$executable = Join-Path $installRoot "DiscDock.exe"
$taskName = "DiscDock"

if (-not (Test-Path -LiteralPath (Join-Path $releaseRoot "DiscDock.exe"))) {
    throw "Build the release first with scripts\build-release.ps1."
}

$healthAvailable = $false
try {
    $bootstrap = Invoke-RestMethod -Uri "http://127.0.0.1:8199/api/v1/bootstrap" -TimeoutSec 2
    $healthAvailable = $true
    $activeStates = @("detected","inspecting","identifying","awaiting_input","queued","ripping","ripped","verifying","transcoding","finalizing","ejecting","cancelling")
    if ($bootstrap.jobs | Where-Object { $_.state -in $activeStates }) { throw "A disc job is active. Finish or stop it before upgrading DiscDock." }
} catch {
    if ($_.Exception.Message -like "A disc job is active*") { throw }
}

if ($InstallMediaTools) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw "Windows Package Manager (winget) is required to install media tools." }
    $packages = @("HandBrake.HandBrake.CLI", "cyanreg.cyanrip")
    foreach ($package in $packages) {
        & $winget.Source install --id $package --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -ne 0) { throw "Could not install $package." }
    }
}

$installedProcesses = @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.Equals($executable, [StringComparison]::OrdinalIgnoreCase) }
)
if ($installedProcesses.Count -gt 0 -and -not $healthAvailable) {
    throw "DiscDock is running, but its safety check did not respond. Close it and retry so an active rip is never interrupted."
}
foreach ($installedProcess in $installedProcesses) {
    Stop-Process -Id $installedProcess.ProcessId -ErrorAction Stop
}
if ($installedProcesses.Count -gt 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = @($installedProcesses | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)
    if ($remaining.Count -gt 0) { throw "DiscDock did not stop cleanly. Restart Windows before upgrading." }
}

$programsRoot = Split-Path -Parent $installRoot
$stagingRoot = Join-Path $programsRoot "DiscDock.installing-$PID"
$backupRoot = Join-Path $programsRoot "DiscDock.previous"
$programsRootFull = [IO.Path]::GetFullPath($programsRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
function Assert-DiscDockSiblingPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $expectedPrefix = $programsRootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a folder outside $programsRootFull"
    }
    $leaf = Split-Path -Leaf $full
    if ($leaf -notlike "DiscDock.*") { throw "Refusing unexpected installation folder: $leaf" }
    return $full
}
$stagingRoot = Assert-DiscDockSiblingPath $stagingRoot
$backupRoot = Assert-DiscDockSiblingPath $backupRoot
New-Item -ItemType Directory -Force -Path $programsRoot | Out-Null
if (Test-Path -LiteralPath $stagingRoot) { Remove-Item -LiteralPath $stagingRoot -Recurse -Force }
New-Item -ItemType Directory -Path $stagingRoot | Out-Null
Copy-Item -Path (Join-Path $releaseRoot "*") -Destination $stagingRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "launch-installed.ps1") -Destination $stagingRoot -Force
if (-not (Test-Path -LiteralPath (Join-Path $stagingRoot "DiscDock.exe"))) {
    throw "The staged installation is incomplete. The current installation was not changed."
}

if ($ArmConfigPath -and (Test-Path -LiteralPath $ArmConfigPath)) {
    $resolvedArmConfig = (Resolve-Path -LiteralPath $ArmConfigPath).Path
    $importArguments = "--import-arm-config `"$resolvedArmConfig`""
    $importProcess = Start-Process -FilePath (Join-Path $stagingRoot "DiscDock.exe") -ArgumentList $importArguments -WindowStyle Hidden -Wait -PassThru
    if ($importProcess.ExitCode -ne 0) { throw "Could not securely import the ARM settings." }
}

if (Test-Path -LiteralPath $backupRoot) { Remove-Item -LiteralPath $backupRoot -Recurse -Force }
if (Test-Path -LiteralPath $installRoot) { Move-Item -LiteralPath $installRoot -Destination $backupRoot }
try {
    Move-Item -LiteralPath $stagingRoot -Destination $installRoot
} catch {
    if (-not (Test-Path -LiteralPath $installRoot) -and (Test-Path -LiteralPath $backupRoot)) {
        Move-Item -LiteralPath $backupRoot -Destination $installRoot
    }
    throw
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not $NoStartup) {
    $action = New-ScheduledTaskAction -Execute $executable -Argument "--background" -WorkingDirectory $installRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $trigger.Delay = "PT10S"
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
# Setup.exe starts DiscDock from the Run key instead. Keep only one way of starting it.
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "DiscDock" -ErrorAction SilentlyContinue

$shell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\DiscDock.lnk"
$shortcut = $shell.CreateShortcut($startMenu)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installRoot\launch-installed.ps1`""
$shortcut.WorkingDirectory = $installRoot
$shortcut.IconLocation = "$executable,0"
$shortcut.Save()

# This launch becomes the running instance for this session. The scheduled
# task is registered for future logons, avoiding a two-process startup race.
Start-Process -FilePath $executable -ArgumentList "--open-browser" -WindowStyle Hidden
Write-Host "DiscDock is installed and will start with Windows. Your media and settings are kept outside the program folder." -ForegroundColor Green
