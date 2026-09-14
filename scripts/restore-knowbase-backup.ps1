#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)][string]$BackupDirectory,
    [string]$WslDistribution = "Ubuntu",
    [string]$ContainerName = "knowbase-pg",
    [ValidateRange(1, 65535)][int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$backupRoot = (Resolve-Path -LiteralPath $BackupDirectory).Path
$dumpPath = Join-Path $backupRoot "knowbase.dump"
$backupStorage = Join-Path $backupRoot "storage"
$storageRoot = Join-Path $projectRoot "data\storage"
$dataRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "data"))
$storageFullPath = [System.IO.Path]::GetFullPath($storageRoot)
$backupStorageFullPath = [System.IO.Path]::GetFullPath($backupStorage)
$dockerModule = Join-Path $PSScriptRoot "KnowBase.WslDocker.psm1"
$containerDump = "/tmp/knowbase-restore.dump"
$restoreDatabase = "knowbase_restore"

if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf)) {
    throw "Database dump not found: $dumpPath"
}
$header = New-Object byte[] 5
$stream = [System.IO.File]::OpenRead($dumpPath)
try {
    $headerLength = $stream.Read($header, 0, $header.Length)
}
finally {
    $stream.Dispose()
}
if ($headerLength -ne 5 -or [Text.Encoding]::ASCII.GetString($header) -ne "PGDMP") {
    throw "The backup is not a PostgreSQL custom-format dump: $dumpPath"
}
if (-not (Test-Path -LiteralPath $backupStorage -PathType Container)) {
    throw "Original document backup not found: $backupStorage"
}
if (
    $storageFullPath -ne [System.IO.Path]::GetFullPath((Join-Path $dataRoot "storage")) -or
    -not $storageFullPath.StartsWith(
        $dataRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "Refusing to replace a storage path outside the project data directory: $storageFullPath"
}
if (
    $backupStorageFullPath -eq $storageFullPath -or
    $backupStorageFullPath.StartsWith(
        $storageFullPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "The backup storage directory must not be the live storage directory: $backupStorageFullPath"
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    if ($health.status -eq "ok") {
        throw "KnowBase API is running. Stop its PowerShell window before restoring the database."
    }
}
catch {
    if ($_.Exception.Message -like "KnowBase API is running.*") {
        throw
    }
}

if (-not $PSCmdlet.ShouldProcess(
    "database 'knowbase' and '$storageRoot'",
    "Restore snapshot '$backupRoot'"
)) {
    return
}

Import-Module -Name $dockerModule -Force
Start-KnowBaseDatabase -ProjectRoot $projectRoot -Distribution $WslDistribution
$wslDumpPath = ConvertTo-KnowBaseWslPath -Distribution $WslDistribution -WindowsPath $dumpPath

Invoke-KnowBaseDocker `
    -Distribution $WslDistribution `
    -ArgumentList @("cp", $wslDumpPath, "${ContainerName}:$containerDump") `
    -FailureMessage "Could not copy the database dump into '$ContainerName'"

$swapStarted = $false
try {
    try {
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @(
                "exec", $ContainerName,
                "dropdb", "--force", "--if-exists", "-U", "postgres", $restoreDatabase
            ) `
            -FailureMessage "Could not clear the staging restore database"
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @("exec", $ContainerName, "createdb", "-U", "postgres", $restoreDatabase) `
            -FailureMessage "Could not create the staging restore database"
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @(
                "exec", $ContainerName,
                "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
                "-U", "postgres", "-d", $restoreDatabase, $containerDump
            ) `
            -FailureMessage "Could not restore the snapshot into the staging database"

        # Keep the current database intact until pg_restore has completed. The swap
        # has a short gap because PostgreSQL cannot rename over an existing database.
        $swapStarted = $true
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @(
                "exec", $ContainerName,
                "dropdb", "--force", "--if-exists", "-U", "postgres", "knowbase"
            ) `
            -FailureMessage "Could not replace the current knowbase database"
        Invoke-KnowBaseDocker `
            -Distribution $WslDistribution `
            -ArgumentList @(
                "exec", $ContainerName,
                "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-c",
                "ALTER DATABASE $restoreDatabase RENAME TO knowbase;"
            ) `
            -FailureMessage "The snapshot was restored, but the staging database could not be renamed to knowbase"
        $swapStarted = $false
    }
    catch {
        if (-not $swapStarted) {
            try {
                Invoke-KnowBaseDocker `
                    -Distribution $WslDistribution `
                    -ArgumentList @(
                        "exec", $ContainerName,
                        "dropdb", "--force", "--if-exists", "-U", "postgres", $restoreDatabase
                    ) `
                    -Quiet
            }
            catch {
                Write-Warning "The failed staging database '$restoreDatabase' could not be removed."
            }
        }
        else {
            Write-Warning "The database swap was interrupted. The restored data remains in '$restoreDatabase'."
        }
        throw
    }
}
finally {
    Invoke-KnowBaseDocker `
        -Distribution $WslDistribution `
        -ArgumentList @("exec", $ContainerName, "rm", "-f", $containerDump) `
        -Quiet `
        -FailureMessage "The database was restored, but the temporary dump could not be removed"
}

$archiveRoot = Join-Path $projectRoot ("data\backups\before-restore-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
if (Test-Path -LiteralPath $storageRoot -PathType Container) {
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    Copy-Item -LiteralPath $storageRoot -Destination (Join-Path $archiveRoot "storage") -Recurse -Force
    Remove-Item -LiteralPath $storageFullPath -Recurse -Force
}
New-Item -ItemType Directory -Path $storageRoot -Force | Out-Null
Copy-Item -Path (Join-Path $backupStorage "*") -Destination $storageRoot -Recurse -Force

Write-Host "[KnowBase] Database and original documents restored from $backupRoot" -ForegroundColor Green
if (Test-Path -LiteralPath $archiveRoot -PathType Container) {
    Write-Host "[KnowBase] Previous original documents archived at $archiveRoot" -ForegroundColor Green
}
