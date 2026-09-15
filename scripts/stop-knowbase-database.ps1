#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$WslDistribution = "Ubuntu",
    [ValidateRange(1, 65535)][int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
Import-Module -Name $dockerModule -Force

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    if ($health.status -eq "ok") {
        throw "KnowBase API is still running. Press Ctrl+C in its PowerShell window before stopping the database."
    }
}
catch {
    if ($_.Exception.Message -like "KnowBase API is still running.*") {
        throw
    }
}

Start-KnowBaseDockerEngine -Distribution $WslDistribution
Invoke-KnowBaseCompose `
    -ProjectRoot $projectRoot `
    -Distribution $WslDistribution `
    -ArgumentList @("stop") `
    -FailureMessage "Could not stop the KnowBase database container"

$wslExe = (Get-Command wsl.exe -ErrorAction Stop).Source
& $wslExe --terminate $WslDistribution *> $null
if ($LASTEXITCODE -ne 0) {
    throw "The container stopped, but WSL distribution '$WslDistribution' could not be terminated."
}
Write-Host "[KnowBase] PostgreSQL stopped and WSL '$WslDistribution' was released." -ForegroundColor Green
