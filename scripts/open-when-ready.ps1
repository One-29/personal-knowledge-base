#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:8000/ui/",
    [string]$HealthUrl = "http://127.0.0.1:8000/health",
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 2
        if ($health.status -eq "ok") {
            Start-Process -FilePath $Url
            exit 0
        }
    }
    catch {
        # The API may still be starting. Retry until the deadline.
    }

    Start-Sleep -Seconds 1
}

exit 1
