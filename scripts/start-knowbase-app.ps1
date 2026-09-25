#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateRange(0, 65535)]
    [int]$Port = 8000,
    [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-LaunchError {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        Add-Type -AssemblyName PresentationFramework
        [void][System.Windows.MessageBox]::Show(
            $Message,
            "KnowBase startup failed",
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        )
    }
    catch {
        Write-Error $Message
    }
}

try {
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $pythonwExe = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
    $envFile = Join-Path $projectRoot ".env"
    $frontendBuilder = Join-Path $PSScriptRoot "build-frontend.ps1"

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pythonwExe -PathType Leaf)) {
        throw "Missing .venv. Follow the README to create it and install the desktop dependencies."
    }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "Missing .env. Copy .env.example and configure the model API keys."
    }
    if (-not (Test-Path -LiteralPath $frontendBuilder -PathType Leaf)) {
        throw "Frontend build script not found: $frontendBuilder"
    }

    & $pythonExe -c "import webview" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Desktop dependency missing. Run: .\.venv\Scripts\python.exe -m pip install -e ".[desktop]"'
    }

    Set-Location -LiteralPath $projectRoot
    & $frontendBuilder
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend preparation failed."
    }

    $desktopArguments = @("-m", "app.desktop", "--port", $Port)
    if ($Check) {
        $desktopArguments += "--check"
    }

    $process = Start-Process `
        -FilePath $pythonwExe `
        -ArgumentList $desktopArguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        exit $process.ExitCode
    }
}
catch {
    if ($Check) {
        [Console]::Error.WriteLine("KnowBase startup failed: {0}", $_.Exception.Message)
    }
    else {
        Show-LaunchError -Message $_.Exception.Message
    }
    exit 1
}
