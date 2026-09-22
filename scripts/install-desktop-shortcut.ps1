#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$ShortcutName = "KnowBase",
    [string]$DesktopPath = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

$launcherPath = Join-Path $ProjectRoot "scripts\start-knowbase-app.ps1"
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Launcher not found: $launcherPath"
}
$pythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pythonwExe = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$frontendBuilder = Join-Path $ProjectRoot "scripts\build-frontend.ps1"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $pythonwExe -PathType Leaf)) {
    throw "Missing .venv. Install the project with the desktop extra first."
}
& $pythonExe -c "import webview" *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Desktop dependency missing. Run: .\.venv\Scripts\python.exe -m pip install -e ".[desktop]"'
}
& $frontendBuilder
if ($LASTEXITCODE -ne 0) {
    throw "Frontend preparation failed."
}

if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
    $DesktopPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
}
if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    throw "Desktop directory not found: $DesktopPath"
}

$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcutPath = Join-Path $DesktopPath "$ShortcutName.lnk"
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Port {1}' -f `
    $launcherPath, $Port

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $windowsPowerShell
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = "Open KnowBase as a desktop application"
$shortcut.IconLocation = "$pythonwExe,0"
$shortcut.Save()

Write-Output $shortcutPath
