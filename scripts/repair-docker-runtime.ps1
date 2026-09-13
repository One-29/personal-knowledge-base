#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Elevated,
    [string]$LogPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "KnowBase-Docker-Recovery.log"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    if ($Elevated) {
        throw "Administrator elevation was requested but is not active."
    }

    $windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Elevated -LogPath "{1}"' -f `
        $PSCommandPath, $LogPath
    $elevatedProcess = Start-Process `
        -FilePath $windowsPowerShell `
        -ArgumentList $arguments `
        -Verb RunAs `
        -Wait `
        -PassThru
    exit $elevatedProcess.ExitCode
}

Start-Transcript -LiteralPath $LogPath -Force | Out-Null
trap {
    Write-Error ($_ | Out-String)
    try {
        Stop-Transcript | Out-Null
    }
    catch {
    }
    exit 1
}

$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$localRoot = [System.IO.Path]::GetFullPath($localAppData)
$runtimeDirectories = @(
    (Join-Path $localRoot "Docker\run"),
    (Join-Path $localRoot "docker-secrets-engine")
)
$moveScheduled = $false

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class KnowBaseDockerRuntimeRecovery
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool MoveFileEx(
        string existingFileName,
        string newFileName,
        int flags
    );
}
'@

Get-Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessName -in @(
            "Docker Desktop",
            "com.docker.backend",
            "com.docker.proxy",
            "docker-desktop",
            "vpnkit"
        )
    } |
    Stop-Process -Force

Start-Sleep -Seconds 3

$remainingDockerProcesses = Get-Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessName -in @(
            "Docker Desktop",
            "com.docker.backend",
            "com.docker.proxy",
            "docker-desktop",
            "vpnkit"
        )
    }
if ($remainingDockerProcesses) {
    throw "Docker processes are still running. Runtime directories were not changed."
}

& wsl.exe --shutdown
if ($LASTEXITCODE -ne 0) {
    throw "wsl --shutdown failed with exit code $LASTEXITCODE."
}
Start-Sleep -Seconds 2

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($runtimeDirectory in $runtimeDirectories) {
    $source = [System.IO.Path]::GetFullPath($runtimeDirectory)
    $destination = "$source.recovery-$timestamp"

    foreach ($candidate in @($source, $destination)) {
        if (-not $candidate.StartsWith(
            $localRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to move a path outside LocalAppData: $candidate"
        }
    }

    if (Test-Path -LiteralPath $source -PathType Container) {
        try {
            Rename-Item `
                -LiteralPath $source `
                -NewName ([System.IO.Path]::GetFileName($destination)) `
                -ErrorAction Stop
            Write-Output "Preserved runtime directory: $destination"
        }
        catch {
            $delayUntilReboot = 4
            $scheduled = [KnowBaseDockerRuntimeRecovery]::MoveFileEx(
                $source,
                $destination,
                $delayUntilReboot
            )
            if (-not $scheduled) {
                $nativeCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                throw "Could not rename or schedule '$source'. Win32 error ${nativeCode}: $([ComponentModel.Win32Exception]::new($nativeCode).Message)"
            }

            $moveScheduled = $true
            Write-Output "Scheduled runtime directory move for the next Windows startup: $source -> $destination"
            continue
        }
    }

    New-Item -ItemType Directory -Path $source -Force -ErrorAction Stop | Out-Null
    Write-Output "Created clean runtime directory: $source"
}

if ($moveScheduled) {
    Write-Warning "Windows must restart once before Docker Desktop can use the repaired runtime paths."
    Stop-Transcript | Out-Null
    exit 2
}

Write-Output "Docker runtime socket recovery completed. Start Docker Desktop again."
Stop-Transcript | Out-Null
