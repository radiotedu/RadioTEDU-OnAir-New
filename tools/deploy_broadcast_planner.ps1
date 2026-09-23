[CmdletBinding()]
param(
    [string]$LiveRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio',
    [string]$ServiceName = 'RadioTEDU.OnAir.Supervisor',
    [string]$Python = 'C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe',
    [string]$WorkspaceRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ResolvedLiveRoot = (Resolve-Path -LiteralPath $LiveRoot).Path
$BackupRoot = Join-Path $WorkspaceRoot 'broadcast-control-20260923\backup\live-runtime-pre-broadcast-planner-v4'
$NewFiles = @(
    'app\api\broadcast_planner.py',
    'app\engine\broadcast_plan_policy.py',
    'app\services\broadcast_planner.py'
)
$Files = @(
    'app\api\broadcast_planner.py',
    'app\auth\dependencies.py',
    'app\db.py',
    'app\engine\broadcast_plan_policy.py',
    'app\engine\broadcast_queue_autofill.py',
    'app\engine\station_worker.py',
    'app\main.py',
    'app\services\broadcast_planner.py',
    'app\static\onair\app.js',
    'app\static\onair\index.html',
    'app\static\onair\styles.css',
    'tools\verify_live_runtime.py'
)
$CopyFromSource = @(
    'app\api\broadcast_planner.py',
    'app\auth\dependencies.py',
    'app\db.py',
    'app\engine\broadcast_plan_policy.py',
    'app\engine\broadcast_queue_autofill.py',
    'app\engine\station_worker.py',
    'app\main.py',
    'app\services\broadcast_planner.py',
    'app\static\onair\app.js',
    'app\static\onair\index.html',
    'app\static\onair\styles.css'
)

function Wait-ServiceState([string]$Name, [string]$State, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service -and [string]$service.Status -eq $State) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Service $Name did not reach $State within $TimeoutSeconds seconds."
}

function Wait-BackendListener([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Get-NetTCPConnection -LocalPort 18110 -State Listen -ErrorAction SilentlyContinue) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw 'RadioTEDU OnAir did not reopen its local listener on port 18110.'
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python runtime not found: $Python" }
if (-not (Test-Path -LiteralPath $ResolvedLiveRoot -PathType Container)) { throw "Live service root not found: $ResolvedLiveRoot" }
$dbBackup = Join-Path $WorkspaceRoot 'broadcast-control-20260923\backup\cleanroom-pre-planner-retry-v4.db.verified.json'
if (-not (Test-Path -LiteralPath $dbBackup -PathType Leaf)) { throw 'Verified pre-deployment database backup is missing.' }
$verified = Get-Content -LiteralPath $dbBackup -Raw | ConvertFrom-Json
if (-not $verified.ok -or $verified.integrity_check -ne 'ok') { throw 'Pre-deployment database backup did not pass integrity verification.' }
foreach ($relative in $CopyFromSource) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $relative) -PathType Leaf)) { throw "Source file is missing: $relative" }
}

New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
foreach ($relative in $Files) {
    $baseline = Join-Path $ResolvedLiveRoot $relative
    $backup = Join-Path $BackupRoot $relative
    if (Test-Path -LiteralPath $baseline -PathType Leaf) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Copy-Item -LiteralPath $baseline -Destination $backup -Force
    } elseif ($relative -notin $NewFiles) {
        throw "Live rollback file is missing before deployment: $relative"
    }
}
$manifest = [ordered]@{
    created_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    live_root = $ResolvedLiveRoot
    database_backup = (Join-Path $WorkspaceRoot 'broadcast-control-20260923\backup\cleanroom-pre-planner-retry-v4.db')
    file_count = $Files.Count
    rollback_files = $Files
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $BackupRoot 'manifest.json') -Encoding UTF8

$serviceStopped = $false
try {
    Stop-Service -Name $ServiceName -Force
    $serviceStopped = $true
    Wait-ServiceState $ServiceName 'Stopped' 60

    $verifyPath = Join-Path $ResolvedLiveRoot 'tools\verify_live_runtime.py'
    $verifyText = [System.IO.File]::ReadAllText($verifyPath)
    $verifyExtendedTimeout = $verifyText.Replace('urlopen(request, timeout=15)', 'urlopen(request, timeout=60)')
    if ($verifyExtendedTimeout -eq $verifyText) { throw 'Could not extend the live runtime API verification timeout.' }
    [System.IO.File]::WriteAllText($verifyPath, $verifyExtendedTimeout, [System.Text.UTF8Encoding]::new($false))
    foreach ($relative in $CopyFromSource) {
        $source = Join-Path $SourceRoot $relative
        $destination = Join-Path $ResolvedLiveRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }

    # Keep the active service's existing station-count fallback behavior.
    $appJs = Join-Path $ResolvedLiveRoot 'app\static\onair\app.js'
    $jsText = [System.IO.File]::ReadAllText($appJs)
    $jsText = $jsText.Replace('${stations.length || 6}', '${stations.length}')
    [System.IO.File]::WriteAllText($appJs, $jsText, [System.Text.UTF8Encoding]::new($false))

    & $Python -m py_compile `
        (Join-Path $ResolvedLiveRoot 'app\api\broadcast_planner.py') `
        (Join-Path $ResolvedLiveRoot 'app\services\broadcast_planner.py') `
        (Join-Path $ResolvedLiveRoot 'app\engine\broadcast_plan_policy.py') `
        (Join-Path $ResolvedLiveRoot 'app\engine\station_worker.py') `
        (Join-Path $ResolvedLiveRoot 'app\engine\broadcast_queue_autofill.py') `
        (Join-Path $ResolvedLiveRoot 'app\main.py') `
        (Join-Path $ResolvedLiveRoot 'app\db.py')
    if ($LASTEXITCODE -ne 0) { throw 'Python syntax validation failed before service start.' }

    Start-Service -Name $ServiceName
    Wait-ServiceState $ServiceName 'Running' 60
    Wait-BackendListener 360

    $importCheck = "import sys;sys.path.insert(0,r'$ResolvedLiveRoot');from app.main import app;assert any(getattr(route,'path','')=='/api/broadcast-plans' for route in app.routes);print('planner route registered')"
    & $Python -c $importCheck
    if ($LASTEXITCODE -ne 0) { throw 'The planner API failed its application import check.' }
        $runtimeDeadline = (Get-Date).AddMinutes(5)
    $mainStation = $null
    $runtimeState = $null
    do {
        $runtime = @(& $Python (Join-Path $ResolvedLiveRoot 'tools\verify_live_runtime.py') 2>&1)
        $runtimeText = $runtime -join [Environment]::NewLine
        try {
            $runtimeState = $runtimeText | ConvertFrom-Json -ErrorAction Stop
            $mainStation = $runtimeState.stations | Where-Object { [int]$_.station_id -eq 4 } | Select-Object -First 1
        } catch {
            $runtimeState = $null
            $mainStation = $null
        }
        if ($mainStation -and $mainStation.running -and $mainStation.worker_running -and $mainStation.program_running -and $mainStation.output_running -and $mainStation.mount_healthy -eq $true) {
            break
        }
        Write-Host 'Waiting for main /radio worker, audio output and mount to become healthy...'
        Start-Sleep -Seconds 12
    } while ((Get-Date) -lt $runtimeDeadline)
    if (-not $mainStation -or -not $mainStation.running -or -not $mainStation.worker_running -or -not $mainStation.program_running -or -not $mainStation.output_running -or $mainStation.mount_healthy -ne $true) {
        throw 'Main /radio station 4 did not return to a healthy running state within five minutes.'
    }$serviceStopped = $false
    [ordered]@{
        ok = $true
        service = $ServiceName
        service_state = (Get-Service -Name $ServiceName).Status.ToString()
        listener_18110 = $true
        planner_route = 'registered'
        runtime_verification = $runtimeState
        rollback_backup = $BackupRoot
        database_backup = (Join-Path $WorkspaceRoot 'broadcast-control-20260923\backup\cleanroom-pre-planner-retry-v4.db')
    } | ConvertTo-Json -Depth 12
} catch {
    if ($serviceStopped) {
        $currentService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($currentService -and [string]$currentService.Status -eq 'Running') {
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            Wait-ServiceState $ServiceName 'Stopped' 60
        }
    }
    foreach ($relative in $Files) {
        $backup = Join-Path $BackupRoot $relative
        $destination = Join-Path $ResolvedLiveRoot $relative
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Copy-Item -LiteralPath $backup -Destination $destination -Force
        } elseif ($relative -in $NewFiles -and (Test-Path -LiteralPath $destination -PathType Leaf)) {
            Remove-Item -LiteralPath $destination -Force
        }
    }
    if ($serviceStopped) { Start-Service -Name $ServiceName -ErrorAction SilentlyContinue }
    throw
}
