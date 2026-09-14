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
    $argumentString = "-d $Distribution -- flock -n $lockPath sleep infinity"
    Start-Process `
        -FilePath $wslExe `
        -ArgumentList $argumentString `
        -WindowStyle Hidden | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        & $wslExe -d $Distribution -- flock -n $lockPath true *> $null
        $probeExitCode = $LASTEXITCODE
        if ($probeExitCode -eq 1) {
            return
        }
        if ($probeExitCode -ne 0) {
            throw "Could not verify the WSL keepalive process (exit code $probeExitCode)."
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "The WSL keepalive process did not acquire its lock within 15 seconds."
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
