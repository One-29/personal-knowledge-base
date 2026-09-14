param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project virtual environment was not found. Run the normal KnowBase setup first."
}

Push-Location $repoRoot
try {
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
