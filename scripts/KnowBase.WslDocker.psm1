#Requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-KnowBaseWslExecutable {
    $command = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "WSL was not found. Install WSL2 and an Ubuntu distribution first."
    }
    return $command.Source
}

function Invoke-KnowBaseWsl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Distribution,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$User = "",
        [switch]$PassThru,
        [switch]$Quiet,
        [string]$FailureMessage = "The WSL command failed"
    )

    $wslExe = Get-KnowBaseWslExecutable
    $wslArguments = @("-d", $Distribution)
    if (-not [string]::IsNullOrWhiteSpace($User)) {
        $wslArguments += @("-u", $User)
    }
    $wslArguments += "--"
    $wslArguments += $ArgumentList

    # Windows PowerShell 5.1 wraps native stderr records as PowerShell errors when
    # stderr is merged into stdout. Compose writes normal progress to stderr, so
    # collect it with Continue and decide success exclusively from the exit code.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $wslExe @wslArguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if (-not $Quiet) {
        foreach ($line in $output) {
            Write-Host $line
        }
    }
    if ($exitCode -ne 0) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        if ([string]::IsNullOrWhiteSpace($detail)) {
            throw "$FailureMessage (exit code $exitCode)."
        }
        throw "$FailureMessage (exit code $exitCode):$([Environment]::NewLine)$detail"
    }
    if ($PassThru) {
        return $output
    }
}

function ConvertTo-KnowBaseWslPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Distribution,
        [Parameter(Mandatory = $true)][string]$WindowsPath
    )

    $resolvedPath = (Resolve-Path -LiteralPath $WindowsPath).Path
    $output = @(Invoke-KnowBaseWsl `
        -Distribution $Distribution `
        -ArgumentList @("wslpath", "-a", "-u", $resolvedPath) `
        -PassThru `
        -Quiet `
        -FailureMessage "Could not convert the Windows path for WSL")
    $path = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    $path = $path.Trim()
    if ([string]::IsNullOrWhiteSpace($path)) {
        throw "WSL returned an empty path for '$resolvedPath'."
    }
    return $path
}

function Start-KnowBaseWslKeepAlive {
    [CmdletBinding()]
    param(
        [ValidatePattern("^[A-Za-z0-9_.-]+$")]
        [string]$Distribution = "Ubuntu"
    )

    $wslExe = Get-KnowBaseWslExecutable
    $lockPath = "/tmp/knowbase-wsl-keepalive.lock"
    $lockConflictExitCode = 75
    $readyPath = "/tmp/knowbase-wsl-keepalive-$([Guid]::NewGuid().ToString('N')).ready"

    # The holder must be the only process that tries to acquire the lock while it
    # is starting. Probing the lock from PowerShell can win the race on a cold WSL
    # boot, causing the non-blocking holder to exit before it ever becomes ready.
    # Instead, the command writes a unique marker only after flock owns the lock.
    $holderCommand = ": > '$readyPath'; exec sleep infinity"
    $argumentString = '-d {0} -- flock -n -E {1} {2} sh -c "{3}"' -f `
        $Distribution, $lockConflictExitCode, $lockPath, $holderCommand
    $keepAliveProcess = Start-Process `
        -FilePath $wslExe `
        -ArgumentList $argumentString `
        -WindowStyle Hidden `
        -PassThru

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    try {
        do {
            & $wslExe -d $Distribution -- test -f $readyPath *> $null
            $readyProbeExitCode = $LASTEXITCODE

            $keepAliveProcess.Refresh()
            if ($readyProbeExitCode -eq 0) {
                if ($keepAliveProcess.HasExited) {
                    throw "The WSL keepalive process exited after acquiring its lock (exit code $($keepAliveProcess.ExitCode))."
                }
                return
            }
            if ($readyProbeExitCode -ne 1) {
                throw "Could not verify the WSL keepalive process (exit code $readyProbeExitCode)."
            }

            if ($keepAliveProcess.HasExited) {
                if ($keepAliveProcess.ExitCode -eq $lockConflictExitCode) {
                    # Another launcher already owns the lock, so its keepalive is
                    # the single active instance and this launch can continue.
                    return
                }
                throw "The WSL keepalive process exited before becoming ready (exit code $($keepAliveProcess.ExitCode))."
            }

            Start-Sleep -Milliseconds 250
        } while ([DateTime]::UtcNow -lt $deadline)

        throw "The WSL keepalive process did not become ready within 15 seconds."
    }
    finally {
        # The marker is unique to this launch, so removing it cannot affect a
        # concurrent launcher or the lock held by the long-running process.
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $wslExe -d $Distribution -- rm -f $readyPath *> $null
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
}

function Start-KnowBaseDockerEngine {
    [CmdletBinding()]
    param([string]$Distribution = "Ubuntu")

    Start-KnowBaseWslKeepAlive -Distribution $Distribution

    Invoke-KnowBaseWsl `
        -Distribution $Distribution `
        -User "root" `
        -ArgumentList @("systemctl", "start", "docker") `
        -Quiet `
        -FailureMessage "Could not start Docker Engine in WSL distribution '$Distribution'"

    $version = @(Invoke-KnowBaseWsl `
        -Distribution $Distribution `
        -ArgumentList @("docker", "info", "--format", "{{.ServerVersion}}") `
        -PassThru `
        -Quiet `
        -FailureMessage "Docker Engine is unavailable to the default WSL user. Run scripts\install-wsl-docker-engine.ps1")
    Write-Host "[KnowBase] WSL Docker Engine $((($version -join '').Trim())) is ready." -ForegroundColor Cyan
}

function Invoke-KnowBaseDocker {
    [CmdletBinding()]
    param(
        [string]$Distribution = "Ubuntu",
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$PassThru,
        [switch]$Quiet,
        [string]$FailureMessage = "The Docker command failed"
    )

    $parameters = @{
        Distribution = $Distribution
        ArgumentList = @("docker") + $ArgumentList
        FailureMessage = $FailureMessage
    }
    if ($PassThru) {
        $parameters.PassThru = $true
    }
    if ($Quiet) {
        $parameters.Quiet = $true
    }
    return Invoke-KnowBaseWsl @parameters
}

function Invoke-KnowBaseCompose {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$Distribution = "Ubuntu",
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [switch]$PassThru,
        [switch]$Quiet,
        [string]$FailureMessage = "The Docker Compose command failed"
    )

    $composeFile = Join-Path $ProjectRoot "compose.yaml"
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
        throw "Compose file not found: $composeFile"
    }
    $wslComposeFile = ConvertTo-KnowBaseWslPath `
        -Distribution $Distribution `
        -WindowsPath $composeFile
    $parameters = @{
        Distribution = $Distribution
        ArgumentList = @("compose", "--file", $wslComposeFile) + $ArgumentList
        FailureMessage = $FailureMessage
    }
    if ($PassThru) {
        $parameters.PassThru = $true
    }
    if ($Quiet) {
        $parameters.Quiet = $true
    }
    return Invoke-KnowBaseDocker @parameters
}

function Start-KnowBaseDatabase {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$Distribution = "Ubuntu",
        [ValidateRange(10, 600)][int]$StartupTimeoutSeconds = 180
    )

    Start-KnowBaseDockerEngine -Distribution $Distribution
    Invoke-KnowBaseCompose `
        -ProjectRoot $ProjectRoot `
        -Distribution $Distribution `
        -ArgumentList @(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            $StartupTimeoutSeconds.ToString()
        ) `
        -FailureMessage "Could not start the KnowBase PostgreSQL container"
}

Export-ModuleMember -Function @(
    "ConvertTo-KnowBaseWslPath",
    "Invoke-KnowBaseCompose",
    "Invoke-KnowBaseDocker",
    "Start-KnowBaseDatabase",
    "Start-KnowBaseDockerEngine",
    "Start-KnowBaseWslKeepAlive"
)
