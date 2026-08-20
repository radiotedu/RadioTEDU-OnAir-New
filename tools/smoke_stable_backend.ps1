param(
    [string]$ExePath = ".\build\backend-publish\RadioTEDU-OnAir-Backend\RadioTEDU-OnAir-Backend.exe",
    [int]$Port = 18100,
    [int]$StartTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$exe = if ([System.IO.Path]::IsPathRooted($ExePath)) {
    [System.IO.Path]::GetFullPath($ExePath)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $root $ExePath))
}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Packaged backend not found: $exe"
}

$smokeRoot = Join-Path (Join-Path $root ".tmp") (
    "stable-smoke-" + [guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null

$environmentNames = @(
    "CLEANROOM_PORT",
    "CLEANROOM_DB_PATH",
    "CLEANROOM_DATA_ROOT",
    "CLEANROOM_TOOLS_DIR",
    "CLEANROOM_USER_CONFIG_ROOT",
    "CLEANROOM_JWT_SECRET_FILE",
    "CLEANROOM_OPEN_PANEL",
    "CLEANROOM_SKIP_STARTUP_AI",
    "CLEANROOM_DISABLE_LIBRARY_WATCHER",
    "CLEANROOM_DISABLE_PRODUCT_CATALOG"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        "Process"
    )
}

$env:CLEANROOM_PORT = [string]$Port
$env:CLEANROOM_DB_PATH = Join-Path $smokeRoot "smoke.db"
$env:CLEANROOM_DATA_ROOT = $smokeRoot
$env:CLEANROOM_TOOLS_DIR = Join-Path $smokeRoot "tools"
$env:CLEANROOM_USER_CONFIG_ROOT = $smokeRoot
$env:CLEANROOM_JWT_SECRET_FILE = Join-Path $smokeRoot "jwt-signing.key"
$env:CLEANROOM_OPEN_PANEL = "0"
$env:CLEANROOM_SKIP_STARTUP_AI = "1"
$env:CLEANROOM_DISABLE_LIBRARY_WATCHER = "1"
$env:CLEANROOM_DISABLE_PRODUCT_CATALOG = "1"

$process = $null
try {
    $process = Start-Process `
        -FilePath $exe `
        -WorkingDirectory (Split-Path -Parent $exe) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $smokeRoot "stdout.log") `
        -RedirectStandardError (Join-Path $smokeRoot "stderr.log")

    $baseUrl = "http://127.0.0.1:$Port"
    $deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod `
                -Uri "$baseUrl/api/health/ready" `
                -TimeoutSec 2
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $health) {
        throw "Packaged backend did not become healthy on port $Port"
    }

    $secondHealth = Invoke-RestMethod `
        -Uri "$baseUrl/api/health/ready" `
        -TimeoutSec 5
    $publicStations = Invoke-RestMethod `
        -Uri "$baseUrl/api/public/stations" `
        -TimeoutSec 5
    if (
        -not $health.backend_instance_id `
        -or $health.backend_instance_id -ne $secondHealth.backend_instance_id
    ) {
        throw "Packaged backend identity was not stable across health endpoints"
    }

    [pscustomobject]@{
        ready = $true
        pid = $process.Id
        backend_instance_id = $health.backend_instance_id
        station_count = @($publicStations.stations).Count
        executable_bytes = (Get-Item -LiteralPath $exe).Length
        smoke_root = $smokeRoot
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $previousEnvironment[$name],
            "Process"
        )
    }
}
