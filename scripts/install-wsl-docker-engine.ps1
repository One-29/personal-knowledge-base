#Requires -Version 5.1

[CmdletBinding()]
param([string]$WslDistribution = "Ubuntu")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$wslExe = (Get-Command wsl.exe -ErrorAction Stop).Source
$installer = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "install-wsl-docker-engine.sh")).Path
$defaultUserOutput = @(& $wslExe -d $WslDistribution -- id -un 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not open WSL distribution '$WslDistribution': $($defaultUserOutput -join [Environment]::NewLine)"
}
$defaultUser = ($defaultUserOutput -join "").Trim()

$wslInstallerOutput = @(& $wslExe -d $WslDistribution -- wslpath -a -u $installer 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Could not convert the installer path for WSL: $($wslInstallerOutput -join [Environment]::NewLine)"
}
$wslInstaller = ($wslInstallerOutput -join "").Trim()

Write-Host "[KnowBase] Installing Docker Engine in WSL '$WslDistribution'..." -ForegroundColor Cyan
& $wslExe -d $WslDistribution -u root -- bash $wslInstaller $defaultUser
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine installation failed with exit code $LASTEXITCODE."
}

# A new WSL login is required before the docker group membership is visible.
Write-Host "[KnowBase] Restarting WSL distribution '$WslDistribution'..." -ForegroundColor Cyan
& $wslExe --terminate $WslDistribution
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine was installed, but WSL could not be restarted automatically. Run: wsl.exe --terminate $WslDistribution"
}
Start-Sleep -Seconds 2

$verificationOutput = @(& $wslExe -d $WslDistribution -- docker version --format "server={{.Server.Version}} client={{.Client.Version}}" 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Docker was installed but the default user cannot access it: $($verificationOutput -join [Environment]::NewLine)"
}
Write-Host "[KnowBase] Independent WSL Docker Engine is ready: $(($verificationOutput -join '').Trim())" -ForegroundColor Green
