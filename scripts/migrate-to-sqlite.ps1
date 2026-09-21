#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$WslDistribution = "Ubuntu",
    [string]$TargetPath = "data\knowbase.db",
    [string]$ReportPath = "data\knowbase-migration-report.json",
    [switch]$ReplaceExisting
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "[KnowBase Migration] $Message" -ForegroundColor Cyan
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
$previousPythonIoEncoding = [Environment]::GetEnvironmentVariable(
    "PYTHONIOENCODING",
    "Process"
)
$locationPushed = $false

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Missing .venv. Run the normal KnowBase setup first."
}
if (-not (Test-Path -LiteralPath $dockerModule -PathType Leaf)) {
    throw "WSL Docker helper module not found: $dockerModule"
}

Import-Module -Name $dockerModule -Force

try {
    Push-Location -LiteralPath $projectRoot
    $locationPushed = $true
    $env:PYTHONIOENCODING = "utf-8"

    Write-Stage "Starting the PostgreSQL source database..."
    Start-KnowBaseDatabase `
        -ProjectRoot $projectRoot `
        -Distribution $WslDistribution

    Write-Stage "Applying the latest PostgreSQL schema..."
    & $pythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL schema migration failed."
    }

    Write-Stage "Creating and verifying the SQLite candidate..."
    $arguments = @(
        "-m", "app.migration.cli",
        "--target", $TargetPath,
        "--report", $ReportPath
    )
    if ($ReplaceExisting) {
        $arguments += "--replace-existing"
    }
    & $pythonExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Data migration failed with exit code $LASTEXITCODE."
    }
}
finally {
    [Environment]::SetEnvironmentVariable(
        "PYTHONIOENCODING",
        $previousPythonIoEncoding,
        "Process"
    )
    if ($locationPushed) {
        Pop-Location
    }
}
