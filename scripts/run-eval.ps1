#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$WslDistribution = "Ubuntu",
    [string]$ContainerName = "knowbase-pg",
    [switch]$Retrieval,
    [Nullable[int]]$TopK = $null,
    [string]$ReportPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "[KnowBase Eval] $Message" -ForegroundColor Cyan
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
$evalStorage = Join-Path $projectRoot "data\eval-storage"
$evalDatabaseUrl = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/knowbase_eval"
$previousDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
$previousStorageDir = [Environment]::GetEnvironmentVariable("STORAGE_DIR", "Process")
$locationPushed = $false

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Missing .venv. Create it and install the project dependencies first."
}
if (-not (Test-Path -LiteralPath $dockerModule -PathType Leaf)) {
    throw "WSL Docker helper module not found: $dockerModule"
}
if ($null -ne $TopK -and $TopK -lt 1) {
    throw "TopK must be a positive integer."
}

Import-Module -Name $dockerModule -Force

try {
    Push-Location -LiteralPath $projectRoot
    $locationPushed = $true

    Write-Stage "Starting the isolated WSL Docker database..."
    Start-KnowBaseDatabase -ProjectRoot $projectRoot -Distribution $WslDistribution

    Write-Stage "Ensuring the isolated database knowbase_eval exists..."
    $databaseExistsOutput = @(Invoke-KnowBaseDocker `
        -Distribution $WslDistribution `
        -ArgumentList @(
            "exec", $ContainerName,
            "psql", "-U", "postgres", "-d", "postgres", "-tAc",
            "SELECT 1 FROM pg_database WHERE datname = 'knowbase_eval'"
        ) `
        -PassThru `
        -Quiet `
        -FailureMessage "Could not inspect PostgreSQL databases in '$ContainerName'")
    $databaseExists = ($databaseExistsOutput -join "").Trim()
    if ($databaseExists -ne "1") {
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @("exec", $ContainerName, "createdb", "-U", "postgres", "knowbase_eval") `
            -FailureMessage "Could not create the knowbase_eval database"
    }

    # These overrides are process-local. Pydantic still reads model credentials from .env,
    # while this script never reads, prints, or rewrites that file.
    $env:DATABASE_URL = $evalDatabaseUrl
    $env:STORAGE_DIR = $evalStorage

    Write-Stage "Applying migrations to knowbase_eval..."
    & $pythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed."
    }

    $evaluationArguments = @("-m", "eval.run_eval")
    if ($Retrieval) {
        $evaluationArguments += "--retrieval"
    }
    if ($null -ne $TopK) {
        $evaluationArguments += @("--top-k", $TopK.ToString())
    }
    if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
        $evaluationArguments += @("--report", $ReportPath)
    }

    Write-Stage "Running the isolated evaluation..."
    & $pythonExe @evaluationArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Evaluation failed with exit code $LASTEXITCODE."
    }
}
finally {
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $previousDatabaseUrl, "Process")
    [Environment]::SetEnvironmentVariable("STORAGE_DIR", $previousStorageDir, "Process")
    if ($locationPushed) {
        Pop-Location
    }
}
