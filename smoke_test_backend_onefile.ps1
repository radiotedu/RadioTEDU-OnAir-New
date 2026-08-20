param(
    [string]$ExePath = ".\\dist\\RadioTEDU-OnAir-Backend.exe",
    [string]$BaseUrl = "http://127.0.0.1:18100",
    [int]$StartTimeoutSec = 25,
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$exeCandidate = $ExePath
if (-not [System.IO.Path]::IsPathRooted($ExePath)) {
    $exeCandidate = Join-Path $root $ExePath
}
if (-not (Test-Path $exeCandidate)) {
    $lastPathFile = Join-Path $root "last_build_path.txt"
    if (Test-Path $lastPathFile) {
        $lastBuiltExe = (Get-Content -Path $lastPathFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($lastBuiltExe -and (Test-Path $lastBuiltExe)) {
            Write-Warning "Default EXE not found. Using last build path from $lastPathFile"
            $exeCandidate = $lastBuiltExe
        }
    }
}

if (-not (Test-Path $exeCandidate)) {
    $candidates = Get-ChildItem -Path $root -Directory -Filter "dist*" -ErrorAction SilentlyContinue `
        | Sort-Object LastWriteTime -Descending `
        | ForEach-Object { Join-Path $_.FullName "RadioTEDU-OnAir-Backend.exe" } `
        | Where-Object { Test-Path $_ }
    if ($candidates -and @($candidates).Count -gt 0) {
        $exeCandidate = @($candidates)[0]
        Write-Warning "Default EXE not found. Using most recent build: $exeCandidate"
    }
}

if (-not (Test-Path $exeCandidate)) {
    throw "Executable not found: $exeCandidate (and no fallback dist executable found)"
}

$exeFull = [System.IO.Path]::GetFullPath((Resolve-Path $exeCandidate))
$exeDir = Split-Path -Parent $exeFull
$endpointFailures = @()
$baseUri = [Uri]$BaseUrl
if ($baseUri.Scheme -ne "http" -or $baseUri.Host -notin @("127.0.0.1", "localhost")) {
    throw "Smoke BaseUrl must be an HTTP loopback address."
}
$portProbe = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    $baseUri.Port)
try {
    $portProbe.Start()
}
catch {
    throw "Smoke port is already in use; refusing to stop or reuse another process: $($baseUri.Port)"
}
finally {
    $portProbe.Stop()
}

$smokeRoot = Join-Path $root ("build\backend-smoke\run-" + [Guid]::NewGuid().ToString("N"))
$dataRoot = Join-Path $smokeRoot "data"
$databasePath = Join-Path $dataRoot "cleanroom-smoke.db"
$userConfigRoot = Join-Path $smokeRoot "user-config"
$toolsRoot = Join-Path $smokeRoot "tools"
New-Item -ItemType Directory -Force -Path $dataRoot, $toolsRoot | Out-Null
$passwordBytes = New-Object byte[] 32
$random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $random.GetBytes($passwordBytes)
}
finally {
    $random.Dispose()
}
$smokePassword = [Convert]::ToBase64String($passwordBytes)

Write-Output "Starting isolated backend smoke process"
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $exeFull
$startInfo.WorkingDirectory = $exeDir
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$startInfo.EnvironmentVariables["CLEANROOM_OPEN_PANEL"] = "0"
$startInfo.EnvironmentVariables["CLEANROOM_HOST"] = "127.0.0.1"
$startInfo.EnvironmentVariables["CLEANROOM_PORT"] = $baseUri.Port.ToString()
$startInfo.EnvironmentVariables["CLEANROOM_DATA_ROOT"] = $dataRoot
$startInfo.EnvironmentVariables["CLEANROOM_DB_PATH"] = $databasePath
$startInfo.EnvironmentVariables["CLEANROOM_USER_CONFIG_ROOT"] = $userConfigRoot
$startInfo.EnvironmentVariables["CLEANROOM_TOOLS_DIR"] = $toolsRoot
$startInfo.EnvironmentVariables["CLEANROOM_INITIAL_ADMIN_PASSWORD"] = $smokePassword
$startInfo.EnvironmentVariables["CLEANROOM_DISABLE_LIBRARY_WATCHER"] = "1"
$startInfo.EnvironmentVariables["CLEANROOM_SKIP_STARTUP_AI"] = "1"
$startInfo.EnvironmentVariables["CLEANROOM_SKIP_WORKER_AUTOSTART"] = "1"
if (-not $KeepRunning) {
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
}
$started = [System.Diagnostics.Process]::Start($startInfo)
$stdoutTask = $null
$stderrTask = $null
if (-not $KeepRunning) {
    $stdoutTask = $started.StandardOutput.ReadToEndAsync()
    $stderrTask = $started.StandardError.ReadToEndAsync()
}
$smokeSucceeded = $false

try {
    $healthUri = "$BaseUrl/api/health/live"
    $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $res = Invoke-WebRequest -Uri $healthUri -UseBasicParsing -Method GET -TimeoutSec 3
            if ([int]$res.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 400
        }
    }

    if (-not $ready) {
        throw "Backend did not become live within $StartTimeoutSec seconds ($healthUri)."
    }

$loginBody = @{
    username = "admin"
    password = $smokePassword
} | ConvertTo-Json -Compress
$login = Invoke-RestMethod `
    -Uri "$BaseUrl/api/auth/login" `
    -UseBasicParsing `
    -Method POST `
    -ContentType "application/json" `
    -Body $loginBody `
    -TimeoutSec 8
$accessToken = [string]$login.access_token
if (-not $accessToken) {
    throw "Isolated smoke login did not return an access token."
}
$authHeaders = @{ Authorization = "Bearer $accessToken" }

function Test-JsonShape {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [string]$Kind = "",
        [string[]]$Required = @()
    )

    if (-not $Kind) {
        return @{ ok = $true; msg = "" }
    }

    $parsed = $null
    try {
        $parsed = $Content | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return @{ ok = $false; msg = "invalid JSON payload" }
    }

    if ($Kind -eq "array") {
        if (-not ($parsed -is [System.Array])) {
            return @{ ok = $false; msg = "expected JSON array" }
        }
        return @{ ok = $true; msg = "" }
    }

    if ($Kind -eq "object") {
        if (-not ($parsed -is [PSCustomObject] -or $parsed -is [hashtable])) {
            return @{ ok = $false; msg = "expected JSON object" }
        }

        $propNames = @()
        if ($parsed -is [hashtable]) {
            $propNames = @($parsed.Keys)
        }
        else {
            $propNames = @($parsed.PSObject.Properties.Name)
        }

        foreach ($name in @($Required)) {
            if ($propNames -notcontains $name) {
                return @{ ok = $false; msg = "missing key '$name'" }
            }
        }
        return @{ ok = $true; msg = "" }
    }

    return @{ ok = $false; msg = "unknown shape kind '$Kind'" }
}

$stationId = 1
try {
    $stationsProbe = Invoke-WebRequest -Uri "$BaseUrl/api/stations" -Headers $authHeaders -UseBasicParsing -Method GET -TimeoutSec 8
    if ([int]$stationsProbe.StatusCode -eq 200) {
        $stationsJson = $stationsProbe.Content | ConvertFrom-Json
        if ($stationsJson -and $stationsJson.stations -and @($stationsJson.stations).Count -gt 0) {
            $candidate = [int](@($stationsJson.stations)[0].id)
            if ($candidate -gt 0) {
                $stationId = $candidate
            }
        }
    }
}
catch {
    # keep default station id
}

$healthProbe = Invoke-RestMethod `
    -Uri "$BaseUrl/api/health?station_id=$stationId" `
    -Headers $authHeaders `
    -UseBasicParsing `
    -Method GET `
    -TimeoutSec 8
$databaseProbe = $healthProbe.database
$databaseCoreHealthy = (
    $databaseProbe `
    -and [string]$databaseProbe.integrity -eq "ok" `
    -and [string]$databaseProbe.journal_mode -eq "wal" `
    -and [bool]$databaseProbe.foreign_keys `
    -and [string]$databaseProbe.synchronous -in @("full", "extra")
)
if (-not $databaseCoreHealthy) {
    throw "Packaged backend database safety checks failed."
}
$diskCritical = (
    [int64]$databaseProbe.disk_free_bytes -lt 512MB `
    -or [double]$databaseProbe.disk_free_percent -lt 3.0
)
if (-not [bool]$databaseProbe.healthy -and -not $diskCritical) {
    throw "Packaged backend reported an unhealthy database for a reason other than the disk reserve gate."
}
if ($diskCritical) {
    Write-Warning "Readiness is intentionally unavailable because the smoke volume is below the disk reserve threshold."
}

$checks = @(
    @{ path = "/"; expect = 200; kind = "" },
    @{ path = "/static/onair/app.js?v=31"; expect = 200; kind = "" },
    @{ path = "/api/health?station_id=$stationId"; expect = 200; kind = "object"; required = @("status", "engine_running", "dependencies") },
    @{ path = "/api/stations"; expect = 200; kind = "object"; required = @("stations") },
    @{ path = "/api/liquidsoap/status?station_id=$stationId"; expect = 200; kind = "object"; required = @("alive", "active_station_id") },
    @{ path = "/api/library/import/ytdlp/jobs/status?limit_recent=25"; expect = 200; kind = "object"; required = @("queue", "recent", "counts") },
    @{ path = "/api/tracks?station_id=$stationId&page=1&per_page=5"; expect = 200; kind = "object"; required = @("tracks", "page", "total_pages") },
    @{ path = "/api/playlists?station_id=$stationId"; expect = 200; kind = "array" },
    @{ path = "/api/playlists?station_id=undefined"; expect = 200; kind = "array" },
    @{ path = "/api/queue?station_id=$stationId"; expect = 200; kind = "object"; required = @("items", "total") },
    @{ path = "/api/program/queue?station_id=$stationId"; expect = 200; kind = "object"; required = @("items", "source", "effective_source") },
    @{ path = "/api/schedule?station_id=$stationId"; expect = 200; kind = "array" },
    @{ path = "/api/schedule/timeline?station_id=$stationId"; expect = 200; kind = "object"; required = @("items", "blocks") },
    @{ path = "/api/ad-break-sets?station_id=$stationId"; expect = 200; kind = "object"; required = @("break_sets") },
    @{ path = "/api/ad-campaigns?station_id=$stationId"; expect = 200; kind = "object"; required = @("campaigns") },
    @{ path = "/api/ads/runtime?station_id=$stationId"; expect = 200; kind = "object"; required = @("due_slots", "next_slots", "history") },
    @{ path = "/api/settings/station?station_id=$stationId"; expect = 200; kind = "object"; required = @("settings", "station") },
    @{ path = "/api/logs?station_id=$stationId&scope=play&per_page=25"; expect = 200; kind = "object"; required = @("logs") },
    @{ path = "/api/music-usage?station_id=$stationId&limit=25"; expect = 200; kind = "object"; required = @("items") },
    @{ path = "/api/music-usage/export?station_id=$stationId&format=csv"; expect = 200; kind = "" },
    @{ path = "/api/music-usage/monthly-closures?limit=12"; expect = 200; kind = "object"; required = @("items") }
)

foreach ($check in $checks) {
    $uri = "$BaseUrl$($check.path)"
    try {
        $res = Invoke-WebRequest -Uri $uri -Headers $authHeaders -UseBasicParsing -Method GET -TimeoutSec 8
        $code = [int]$res.StatusCode
        if ($code -ne [int]$check.expect) {
            $endpointFailures += "GET $($check.path) => $code (expected $($check.expect))"
        }
        else {
            $shapeResult = Test-JsonShape -Content $res.Content -Kind ([string]$check.kind) -Required @($check.required)
            if (-not $shapeResult.ok) {
                $endpointFailures += "GET $($check.path) => shape error ($($shapeResult.msg))"
            }
            else {
                Write-Output "OK  GET $($check.path) => $code"
            }
        }
    }
    catch {
        if ($_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode.value__
            $endpointFailures += "GET $($check.path) => $code (expected $($check.expect))"
        }
        else {
            $endpointFailures += "GET $($check.path) => ERROR ($($_.Exception.Message))"
        }
    }
}

    if ($endpointFailures.Count -gt 0) {
        $failureSummary = $endpointFailures | ForEach-Object { " - $_" }
        throw ("Smoke test failures:`r`n" + ($failureSummary -join "`r`n"))
    }

    Write-Output ""
    Write-Output "Smoke test passed."
    $smokeSucceeded = $true
    if ($KeepRunning) {
        Write-Output "Process left running (PID=$($started.Id))."
    }
}
finally {
    if (-not $KeepRunning) {
        if ($started -and -not $started.HasExited) {
            Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue
        }
        if ($started) {
            $started.WaitForExit(5000) | Out-Null
        }
        $stdoutPath = Join-Path $smokeRoot "backend.stdout.log"
        $stderrPath = Join-Path $smokeRoot "backend.stderr.log"
        if ($stdoutTask) {
            [System.IO.File]::WriteAllText($stdoutPath, $stdoutTask.GetAwaiter().GetResult())
        }
        if ($stderrTask) {
            [System.IO.File]::WriteAllText($stderrPath, $stderrTask.GetAwaiter().GetResult())
        }

        $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
        $allowedSmokeRoot = [System.IO.Path]::GetFullPath((Join-Path $root "build\backend-smoke"))
        if ($resolvedSmokeRoot.StartsWith($allowedSmokeRoot + [System.IO.Path]::DirectorySeparatorChar)) {
            if ($smokeSucceeded) {
                Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
            }
            else {
                Write-Warning "Smoke diagnostics retained at: $resolvedSmokeRoot"
            }
        }
    }
}
