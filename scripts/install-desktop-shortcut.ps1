#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$ShortcutName = "KnowBase",
    [string]$DesktopPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

$launcherPath = Join-Path $ProjectRoot "scripts\start-knowbase.ps1"
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Launcher not found: $launcherPath"
}

if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
    $DesktopPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
}
if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
    throw "Desktop directory not found: $DesktopPath"
}

$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcutPath = Join-Path $DesktopPath "$ShortcutName.lnk"
$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $launcherPath

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $windowsPowerShell
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 1
$shortcut.Description = "Start the KnowBase personal knowledge base"
$shortcut.IconLocation = "$windowsPowerShell,0"
$shortcut.Save()

Write-Output $shortcutPath
