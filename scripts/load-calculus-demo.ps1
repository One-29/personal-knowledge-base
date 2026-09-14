[CmdletBinding()]
param([string]$WslDistribution = "Ubuntu")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project virtual environment was not found. Run the normal KnowBase setup first."
}
if (-not (Test-Path -LiteralPath $dockerModule -PathType Leaf)) {
    throw "WSL Docker helper module not found: $dockerModule"
}

Import-Module -Name $dockerModule -Force

Push-Location $repoRoot
try {
    Start-KnowBaseDatabase -ProjectRoot $repoRoot -Distribution $WslDistribution
    & $pythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed."
    }
    & $pythonExe -m demo.load_calculus
    if ($LASTEXITCODE -ne 0) {
        throw "Calculus demo import failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
