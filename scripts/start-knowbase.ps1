#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$WslDistribution = "Ubuntu",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [ValidateRange(10, 600)]
    [int]$DockerStartupTimeoutSeconds = 180,
    [switch]$NoBrowser,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Stage {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "[KnowBase] $Message" -ForegroundColor Cyan
}

function Test-KnowBaseReady {
    param([Parameter(Mandatory = $true)][string]$ReadyUrl)

    try {
        $readiness = Invoke-RestMethod -Uri $ReadyUrl -Method Get -TimeoutSec 2
        return $readiness.status -eq "ok" -and $readiness.database -eq "ok"
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
    $readyUrl = "http://127.0.0.1:$Port/ready"
    $appUrl = "http://127.0.0.1:$Port/ui/"
    $dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
    $frontendBuilder = Join-Path $PSScriptRoot "build-frontend.ps1"

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]'"
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Missing .env. Copy .env.example to .env and configure the model API keys first."
    }
    if (-not (Test-Path -LiteralPath $dockerModule -PathType Leaf)) {
        throw "WSL Docker helper module not found: $dockerModule"
    }
    if (-not (Test-Path -LiteralPath $frontendBuilder -PathType Leaf)) {
        throw "Frontend build helper not found: $frontendBuilder"
    }

    Import-Module -Name $dockerModule -Force

    Write-Stage "Preparing the TypeScript frontend..."
    & $frontendBuilder
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend preparation failed."
    }

    Write-Stage "Starting PostgreSQL with Docker Engine in WSL '$WslDistribution'..."
    Start-KnowBaseDatabase `
        -ProjectRoot $projectRoot `
        -Distribution $WslDistribution `
        -StartupTimeoutSeconds $DockerStartupTimeoutSeconds

    if (Test-KnowBaseReady -ReadyUrl $readyUrl) {
        Write-Stage "KnowBase is already running at $appUrl"
        if (-not $NoBrowser) {
            Start-Process -FilePath $appUrl
        }
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

    if (-not $NoBrowser) {
        $browserHelper = Join-Path $PSScriptRoot "open-when-ready.ps1"
        $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $browserArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Url "{1}" -HealthUrl "{2}"' -f `
            $browserHelper, $appUrl, $readyUrl
        Start-Process `
            -FilePath $windowsPowerShell `
            -ArgumentList $browserArguments `
            -WindowStyle Hidden | Out-Null
    }

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
