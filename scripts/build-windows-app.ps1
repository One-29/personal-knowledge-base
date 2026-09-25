#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [string]$OutputDirectory = "",
    [switch]$SkipFrontendBuild,
    [switch]$SkipSmokeTest,
    [switch]$SkipArchive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Windows app packages must be built on Windows."
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
$python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $projectRoot "artifacts\windows"
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$workRoot = Join-Path $projectRoot "build\pyinstaller"
$specFile = Join-Path $projectRoot "packaging\windows\KnowBase.spec"
$frontendBuilder = Join-Path $PSScriptRoot "build-frontend.ps1"

& $python -c "import PyInstaller, webview" *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'Packaging dependencies are missing. Run: .\.venv\Scripts\python.exe -m pip install -e ".[package]"'
}

if (-not $SkipFrontendBuild) {
    & $frontendBuilder
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed."
    }
}

$env:PYTHONUTF8 = "1"
Write-Host "[KnowBase] Building the Windows desktop bundle..." -ForegroundColor Cyan
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $outputRoot `
    --workpath $workRoot `
    $specFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$bundle = Join-Path $outputRoot "KnowBase"
$executable = Join-Path $bundle "KnowBase.exe"
$internal = Join-Path $bundle "_internal"
$requiredFiles = @(
    $executable,
    (Join-Path $internal "frontend\dist\index.html"),
    (Join-Path $internal ".env.example")
)
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Package verification failed; missing file: $required"
    }
}

Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\windows\Configure KnowBase.cmd") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\windows\README.txt") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination (Join-Path $bundle "LICENSE.txt") -Force

$secretFiles = @(
    Get-ChildItem -LiteralPath $bundle -Recurse -File | Where-Object {
        $_.Name -eq ".env" -or $_.Name -eq "config.env"
    }
)
if ($secretFiles.Count -gt 0) {
    throw "Package verification failed: a runtime configuration file was included."
}

if (-not $SkipSmokeTest) {
    $smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("KnowBase-package-check-" + [Guid]::NewGuid().ToString("N"))
    $profileRoot = Join-Path $smokeRoot "profile"
    $configuredProfileRoot = Join-Path $smokeRoot "profile-from-config"
    $configFile = Join-Path $smokeRoot "configuration\config.env"
    $originalDataDir = [Environment]::GetEnvironmentVariable("KNOWBASE_DATA_DIR", "Process")
    $originalConfigFile = [Environment]::GetEnvironmentVariable("KNOWBASE_CONFIG_FILE", "Process")
    $originalPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
    $originalPythonHome = [Environment]::GetEnvironmentVariable("PYTHONHOME", "Process")
    $originalPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    try {
        [System.IO.Directory]::CreateDirectory($smokeRoot) | Out-Null
        [Environment]::SetEnvironmentVariable("KNOWBASE_DATA_DIR", $profileRoot, "Process")
        [Environment]::SetEnvironmentVariable("KNOWBASE_CONFIG_FILE", $configFile, "Process")
        [Environment]::SetEnvironmentVariable("PATH", "$env:SystemRoot\System32;$env:SystemRoot", "Process")
        [Environment]::SetEnvironmentVariable("PYTHONHOME", $null, "Process")
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $null, "Process")
        $process = Start-Process `
            -FilePath $executable `
            -ArgumentList @("--check", "--port", "0") `
            -WorkingDirectory $smokeRoot `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Packaged runtime check failed with exit code $($process.ExitCode)."
        }
        if (-not (Test-Path -LiteralPath (Join-Path $profileRoot "knowbase.db") -PathType Leaf)) {
            throw "Packaged runtime check did not create the isolated SQLite database."
        }
        if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) {
            throw "Packaged runtime check did not create the isolated user configuration."
        }

        $configDataPath = $configuredProfileRoot.Replace("\", "/")
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText(
            $configFile,
            "`nKNOWBASE_DATA_DIR=$configDataPath`n",
            $utf8NoBom
        )
        [Environment]::SetEnvironmentVariable("KNOWBASE_DATA_DIR", $null, "Process")
        $configuredProcess = Start-Process `
            -FilePath $executable `
            -ArgumentList @("--check", "--port", "0") `
            -WorkingDirectory $smokeRoot `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($configuredProcess.ExitCode -ne 0 -or
            -not (Test-Path -LiteralPath (Join-Path $configuredProfileRoot "knowbase.db") -PathType Leaf)) {
            throw "Packaged runtime did not load KNOWBASE_DATA_DIR from the isolated user configuration."
        }

        $windowProcess = Start-Process `
            -FilePath $executable `
            -ArgumentList @("--window-check", "--port", "0") `
            -WorkingDirectory $smokeRoot `
            -WindowStyle Hidden `
            -PassThru
        if (-not $windowProcess.WaitForExit(30000)) {
            Stop-Process -Id $windowProcess.Id -Force
            throw "Packaged WebView check did not finish within 30 seconds."
        }
        if ($windowProcess.ExitCode -ne 0) {
            throw "Packaged WebView check failed with exit code $($windowProcess.ExitCode)."
        }
    }
    catch {
        $smokeLog = Join-Path $configuredProfileRoot "logs\knowbase.log"
        if (-not (Test-Path -LiteralPath $smokeLog -PathType Leaf)) {
            $smokeLog = Join-Path $profileRoot "logs\knowbase.log"
        }
        if (Test-Path -LiteralPath $smokeLog -PathType Leaf) {
            Write-Warning "Packaged smoke log (last 80 lines):"
            Get-Content -LiteralPath $smokeLog -Tail 80 | Write-Warning
        }
        throw
    }
    finally {
        [Environment]::SetEnvironmentVariable("KNOWBASE_DATA_DIR", $originalDataDir, "Process")
        [Environment]::SetEnvironmentVariable("KNOWBASE_CONFIG_FILE", $originalConfigFile, "Process")
        [Environment]::SetEnvironmentVariable("PATH", $originalPath, "Process")
        [Environment]::SetEnvironmentVariable("PYTHONHOME", $originalPythonHome, "Process")
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $originalPythonPath, "Process")
        $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $resolvedSmoke = [System.IO.Path]::GetFullPath($smokeRoot)
        if ($resolvedSmoke.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
            ([System.IO.Path]::GetFileName($resolvedSmoke)).StartsWith("KnowBase-package-check-", [System.StringComparison]::Ordinal)) {
            if ([System.IO.Directory]::Exists($resolvedSmoke)) {
                [System.IO.Directory]::Delete($resolvedSmoke, $true)
            }
        }
    }
}

$version = (& $python -c "from importlib.metadata import version; print(version('knowbase'))").Trim()
if (-not $SkipArchive) {
    $archive = Join-Path $outputRoot "KnowBase-$version-windows-x64.zip"
    Compress-Archive -LiteralPath $bundle -DestinationPath $archive -CompressionLevel Optimal -Force
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $entries = @($zip.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($requiredEntry in @(
            "KnowBase/KnowBase.exe",
            "KnowBase/_internal/frontend/dist/index.html",
            "KnowBase/_internal/.env.example"
        )) {
            if ($requiredEntry -notin $entries) {
                throw "Archive verification failed; missing entry: $requiredEntry"
            }
        }
        $archiveSecrets = @($entries | Where-Object {
            $_ -match "(^|/)(\.env|config\.env)$"
        })
        if ($archiveSecrets.Count -gt 0) {
            throw "Archive verification failed: a runtime configuration file was included."
        }
    }
    finally {
        $zip.Dispose()
    }
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    $checksum = "$archiveHash *$([System.IO.Path]::GetFileName($archive))"
    Set-Content -LiteralPath "$archive.sha256" -Value $checksum -Encoding ASCII
    Write-Output $archive
    Write-Output "$archive.sha256"
}
Write-Output $executable
