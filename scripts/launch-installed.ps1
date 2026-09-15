$ErrorActionPreference = "Stop"
$executable = Join-Path $PSScriptRoot "DiscDock.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "DiscDock.exe is missing from $PSScriptRoot" }
Start-Process -FilePath $executable -ArgumentList "--open-browser" -WindowStyle Hidden
