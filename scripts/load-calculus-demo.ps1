[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project virtual environment was not found. Run the normal KnowBase setup first."
}

Push-Location $repoRoot
try {
    & $pythonExe -m app.runtime --project-root $repoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "SQLite runtime preparation failed."
    }
    & $pythonExe -m demo.load_calculus
    if ($LASTEXITCODE -ne 0) {
        throw "Calculus demo import failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
