#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-TreeFingerprint {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][System.IO.FileInfo[]]$Files
    )

    $builder = New-Object System.Text.StringBuilder
    foreach ($file in ($Files | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($ProjectRoot.Length).TrimStart('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        [void]$builder.AppendLine("$relative`t$hash")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($builder.ToString())
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    }
    finally {
        $sha.Dispose()
    }
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$packageLock = Join-Path $projectRoot "package-lock.json"
$nodeModules = Join-Path $projectRoot "node_modules"
$dependencyStamp = Join-Path $nodeModules ".knowbase-package-lock.sha256"
$frontendDist = Join-Path $projectRoot "frontend\dist"
$buildStamp = Join-Path $frontendDist ".knowbase-build.sha256"

if (-not (Test-Path -LiteralPath $packageLock -PathType Leaf)) {
    throw "Missing package-lock.json. Restore the repository files before building the frontend."
}

$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
}
if ($null -eq $nodeCommand -or $null -eq $npmCommand) {
    throw "Node.js 22.12 or newer is required. Install the current Node.js LTS release and run again."
}

$nodeVersionText = (& $nodeCommand.Source --version).Trim().TrimStart('v')
$nodeVersion = $null
if (-not [System.Version]::TryParse($nodeVersionText, [ref]$nodeVersion)) {
    throw "Unable to read the installed Node.js version: $nodeVersionText"
}
if ($nodeVersion -lt [System.Version]"22.12.0") {
    throw "Node.js 22.12 or newer is required; installed version is $nodeVersionText."
}

$lockHash = (Get-FileHash -LiteralPath $packageLock -Algorithm SHA256).Hash
$installedLockHash = if (Test-Path -LiteralPath $dependencyStamp -PathType Leaf) {
    (Get-Content -LiteralPath $dependencyStamp -Raw).Trim()
} else {
    ""
}
if ($Force -or -not (Test-Path -LiteralPath $nodeModules -PathType Container) -or $installedLockHash -ne $lockHash) {
    Write-Host "[KnowBase] Installing locked frontend dependencies..." -ForegroundColor Cyan
    Push-Location -LiteralPath $projectRoot
    try {
        & $npmCommand.Source ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci failed with exit code $LASTEXITCODE."
        }
        Set-Content -LiteralPath $dependencyStamp -Value $lockHash -Encoding ASCII
    }
    finally {
        Pop-Location
    }
}

$sourceFiles = @(
    Get-Item -LiteralPath (Join-Path $projectRoot "package.json")
    Get-Item -LiteralPath $packageLock
    Get-Item -LiteralPath (Join-Path $projectRoot "vite.config.ts")
    Get-Item -LiteralPath (Join-Path $projectRoot "frontend\tsconfig.json")
    Get-Item -LiteralPath (Join-Path $projectRoot "frontend\index.html")
    Get-ChildItem -LiteralPath (Join-Path $projectRoot "frontend\src") -Recurse -File
    Get-ChildItem -LiteralPath (Join-Path $projectRoot "frontend\public") -Recurse -File
)
$sourceHash = Get-TreeFingerprint -ProjectRoot $projectRoot -Files $sourceFiles
$builtHash = if (Test-Path -LiteralPath $buildStamp -PathType Leaf) {
    (Get-Content -LiteralPath $buildStamp -Raw).Trim()
} else {
    ""
}

if (-not $Force -and $sourceHash -eq $builtHash -and (Test-Path -LiteralPath (Join-Path $frontendDist "index.html") -PathType Leaf)) {
    Write-Host "[KnowBase] Frontend build is current." -ForegroundColor DarkGray
    exit 0
}

Write-Host "[KnowBase] Building the TypeScript frontend..." -ForegroundColor Cyan
Push-Location -LiteralPath $projectRoot
try {
    & $npmCommand.Source run frontend:build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE."
    }
    Set-Content -LiteralPath $buildStamp -Value $sourceHash -Encoding ASCII
}
finally {
    Pop-Location
}
