#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ContainerName = "knowbase-pg",
    [switch]$Retrieval,
    [Nullable[int]]$TopK = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "[KnowBase Eval] $Message" -ForegroundColor Cyan
}

function Get-DockerExecutable {
    $dockerFromPath = Get-Command docker.exe -ErrorAction SilentlyContinue
    $pathCandidate = if ($null -ne $dockerFromPath) { $dockerFromPath.Source } else { $null }
    $candidates = @(
        $pathCandidate,
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )

    foreach ($candidate in $candidates) {
        if (
            -not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)
        ) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Docker CLI was not found. Start Docker Desktop and try again."
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)][string]$DockerExe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $DockerExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Test-DockerEngine {
    param([Parameter(Mandatory = $true)][string]$DockerExe)

    $probe = $null
    try {
        $probe = Start-Process `
            -FilePath $DockerExe `
            -ArgumentList @("info", "--format", "{{.ServerVersion}}") `
            -WindowStyle Hidden `
            -PassThru
        if (-not $probe.WaitForExit(5000)) {
            try {
                $probe.Kill()
                $probe.WaitForExit()
            }
            catch {
                # The Docker probe may have exited between the timeout and Kill().
            }
            return $false
        }
        return $probe.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $probe) {
            $probe.Dispose()
        }
    }
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$evalStorage = Join-Path $projectRoot "data\eval-storage"
$evalDatabaseUrl = "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval"
$previousDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
$previousStorageDir = [Environment]::GetEnvironmentVariable("STORAGE_DIR", "Process")
$locationPushed = $false

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Missing .venv. Create it and install the project dependencies first."
}
if ($null -ne $TopK -and $TopK -lt 1) {
    throw "TopK must be a positive integer."
}

$dockerExe = Get-DockerExecutable

try {
    Push-Location -LiteralPath $projectRoot
    $locationPushed = $true

    if (-not (Test-DockerEngine -DockerExe $dockerExe)) {
        throw "Docker Engine is not ready. Start Docker Desktop and try again."
    }

    $allContainers = @(& $dockerExe container ls --all --format "{{.Names}}")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list Docker containers."
    }
    if ($allContainers -notcontains $ContainerName) {
        throw "PostgreSQL container '$ContainerName' does not exist. Run the KnowBase desktop shortcut once first."
    }

    $containerRunningOutput = @(& $dockerExe inspect --format "{{.State.Running}}" $ContainerName)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect PostgreSQL container '$ContainerName'."
    }
    $containerRunning = ($containerRunningOutput -join "").Trim()
    if ($containerRunning -ne "true") {
        Write-Stage "Starting PostgreSQL container '$ContainerName'..."
        Invoke-Docker `
            -DockerExe $dockerExe `
            -Arguments @("container", "start", $ContainerName) `
            -FailureMessage "Could not start PostgreSQL container '$ContainerName'."
    }

    $postgresDeadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        & $dockerExe exec $ContainerName pg_isready -U postgres -d postgres *> $null
        if ($LASTEXITCODE -eq 0) {
            break
        }
        if ([DateTime]::UtcNow -ge $postgresDeadline) {
            throw "PostgreSQL did not become ready within 60 seconds."
        }
        Start-Sleep -Seconds 1
    } while ($true)

    Write-Stage "Ensuring the isolated database knowbase_eval exists..."
    $databaseExistsOutput = @(& $dockerExe exec $ContainerName psql -U postgres -d postgres -tAc `
        "SELECT 1 FROM pg_database WHERE datname = 'knowbase_eval'")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect PostgreSQL databases in '$ContainerName'."
    }
    $databaseExists = ($databaseExistsOutput -join "").Trim()
    if ($databaseExists -ne "1") {
        Invoke-Docker `
            -DockerExe $dockerExe `
            -Arguments @("exec", $ContainerName, "createdb", "-U", "postgres", "knowbase_eval") `
            -FailureMessage "Could not create the knowbase_eval database."
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
