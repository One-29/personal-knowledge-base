#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$DockerDesktopRoot = "",
    [string]$ContainerName = "knowbase-pg",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [ValidateRange(10, 600)]
    [int]$DockerStartupTimeoutSeconds = 180,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "[KnowBase] $Message" -ForegroundColor Cyan
}

function Get-FirstExistingFile {
    param([string[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if (
            -not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)
        ) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    return $null
}

function Test-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1000, 30000)][int]$TimeoutMilliseconds = 5000
    )

    $process = $null
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -PassThru
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try {
                $process.Kill()
                $process.WaitForExit()
            }
            catch {
                # The probe may have exited between the timeout and Kill().
            }
            return $false
        }
        return $process.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Test-DockerEngine {
    param([Parameter(Mandatory = $true)][string]$DockerExe)

    return Test-NativeCommand `
        -FilePath $DockerExe `
        -Arguments @("info", "--format", "{{.ServerVersion}}")
}

function Test-PostgresReady {
    param(
        [Parameter(Mandatory = $true)][string]$DockerExe,
        [Parameter(Mandatory = $true)][string]$Container
    )

    return Test-NativeCommand `
        -FilePath $DockerExe `
        -Arguments @("exec", $Container, "pg_isready", "-U", "postgres", "-d", "knowbase")
}

function Test-KnowBaseHealth {
    param([Parameter(Mandatory = $true)][string]$HealthUrl)

    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 2
        return $health.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Start-KnowBase {
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $projectRoot

    $pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $envFile = Join-Path $projectRoot ".env"
    $healthUrl = "http://127.0.0.1:$Port/health"
    $appUrl = "http://127.0.0.1:$Port/ui/"

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]'"
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Missing .env. Copy .env.example to .env and configure the model API keys first."
    }

    if ([string]::IsNullOrWhiteSpace($DockerDesktopRoot)) {
        $DockerDesktopRoot = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop"
    }

    $dockerFromPath = Get-Command docker.exe -ErrorAction SilentlyContinue
    $dockerPathCandidate = if ($null -ne $dockerFromPath) { $dockerFromPath.Source } else { $null }
    $dockerExe = Get-FirstExistingFile @(
        (Join-Path $DockerDesktopRoot "resources\bin\docker.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"),
        $dockerPathCandidate
    )
    if ($null -eq $dockerExe) {
        throw "Docker CLI was not found. Install Docker Desktop or pass -DockerDesktopRoot with its installation directory."
    }

    $dockerDesktopExe = Get-FirstExistingFile @(
        (Join-Path $DockerDesktopRoot "Docker Desktop.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe")
    )

    if (-not (Test-DockerEngine -DockerExe $dockerExe)) {
        if ($null -eq $dockerDesktopExe) {
            throw "Docker Engine is not running, and Docker Desktop.exe could not be found."
        }

        Write-Stage "Starting Docker Desktop..."
        Start-Process -FilePath $dockerDesktopExe -WindowStyle Hidden | Out-Null

        $deadline = [DateTime]::UtcNow.AddSeconds($DockerStartupTimeoutSeconds)
        while (-not (Test-DockerEngine -DockerExe $dockerExe)) {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Docker Desktop did not become ready within $DockerStartupTimeoutSeconds seconds. If its dialog mentions an inaccessible *.sock file, run scripts\repair-docker-runtime.ps1 once and accept the UAC prompt."
            }
            Start-Sleep -Seconds 2
        }
    }
    Write-Stage "Docker Engine is ready."

    $allContainers = @(& $dockerExe container ls --all --format "{{.Names}}")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list Docker containers."
    }

    if ($allContainers -notcontains $ContainerName) {
        Write-Stage "Creating PostgreSQL container '$ContainerName'. The first image download can take several minutes."
        & $dockerExe run --detach `
            --name $ContainerName `
            --env POSTGRES_PASSWORD=postgres `
            --env POSTGRES_DB=knowbase `
            --env POSTGRES_HOST_AUTH_METHOD=trust `
            --publish 5432:5432 `
            --volume knowbase_pgdata:/var/lib/postgresql/data `
            pgvector/pgvector:pg16
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create PostgreSQL container '$ContainerName'. Check whether port 5432 is already in use."
        }
    }
    else {
        $runningContainers = @(& $dockerExe container ls --format "{{.Names}}")
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inspect running Docker containers."
        }
        if ($runningContainers -notcontains $ContainerName) {
            Write-Stage "Starting PostgreSQL container '$ContainerName'..."
            & $dockerExe container start $ContainerName | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "Could not start PostgreSQL container '$ContainerName'."
            }
        }
    }

    Write-Stage "Waiting for PostgreSQL..."
    $databaseDeadline = [DateTime]::UtcNow.AddSeconds(60)
    while (-not (Test-PostgresReady -DockerExe $dockerExe -Container $ContainerName)) {
        if ([DateTime]::UtcNow -ge $databaseDeadline) {
            throw "PostgreSQL did not become ready within 60 seconds."
        }
        Start-Sleep -Seconds 1
    }

    if (Test-KnowBaseHealth -HealthUrl $healthUrl) {
        Write-Stage "KnowBase is already running. Opening $appUrl"
        Start-Process -FilePath $appUrl
        return
    }

    $portListener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($null -ne $portListener) {
        throw "Port $Port is already in use by another application. Close it or launch with a different -Port value."
    }

    Write-Stage "Applying database migrations..."
    & $pythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Database migration failed."
    }

    $browserHelper = Join-Path $PSScriptRoot "open-when-ready.ps1"
    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $browserArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Url "{1}" -HealthUrl "{2}"' -f `
        $browserHelper, $appUrl, $healthUrl
    Start-Process `
        -FilePath $windowsPowerShell `
        -ArgumentList $browserArguments `
        -WindowStyle Hidden | Out-Null

    Write-Stage "Starting KnowBase at $appUrl"
    Write-Host "Keep this window open. Press Ctrl+C to stop the API." -ForegroundColor Yellow
    & $pythonExe -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1

    if ($LASTEXITCODE -ne 0) {
        throw "KnowBase API exited with code $LASTEXITCODE."
    }
}

try {
    Start-KnowBase
}
catch {
    Write-Host ""
    Write-Host "[KnowBase] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $NoPause -and [Environment]::UserInteractive) {
        [void](Read-Host "Press Enter to close")
    }
    exit 1
}
