param(
    [int]$Port = 18110,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtimeLauncher = Join-Path $repoRoot "tools\start_radio_backend.ps1"
$url = "http://127.0.0.1:$Port/"
$liveUrl = "http://127.0.0.1:$Port/api/health/live"

if (-not (Test-Path -LiteralPath $runtimeLauncher -PathType Leaf)) {
    throw "The migrated RadioTEDU OnAir launcher is missing: $runtimeLauncher"
}
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    try {
        $health = Invoke-RestMethod -Uri $liveUrl -Method Get -TimeoutSec 3
    } catch {
        throw "Port $Port is already used by another or unhealthy program. RadioTEDU OnAir was not changed."
    }
    if ([string]$health.service -ne "radiotedu-onair" -or [string]$health.status -ne "ok") {
        throw "Port $Port is already used by another program. RadioTEDU OnAir was not changed."
    }
} else {
    & $runtimeLauncher -Port $Port | Out-Null
}

$deadline = (Get-Date).AddSeconds(45)
do {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            if (-not $NoOpen) {
                Start-Process $url
            }
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ((Get-Date) -lt $deadline)

throw "RadioTEDU OnAir did not become ready at $url within 45 seconds."
