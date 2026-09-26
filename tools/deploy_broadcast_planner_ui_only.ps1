[CmdletBinding()]
param(
    [string]$LiveRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio',
    [string]$ServiceName = 'RadioTEDU.OnAir.Supervisor',
    [string]$WorkspaceRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir',
    [string]$DatabasePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this deployment from an elevated PowerShell window.'
}

$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$resolvedLiveRoot = (Resolve-Path -LiteralPath $LiveRoot).Path
$resolvedWorkspaceRoot = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$databasePathResolved = if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    Join-Path $env:ProgramData 'RadioTEDU\OnAir\cleanroom.db'
} else {
    (Resolve-Path -LiteralPath $DatabasePath).Path
}
$requiredLiveRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio'
if (-not [string]::Equals($resolvedLiveRoot, $requiredLiveRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected live runtime path: $resolvedLiveRoot"
}
if (-not (Test-Path -LiteralPath $databasePathResolved -PathType Leaf)) {
    throw "Database file was not found at $databasePathResolved. Pass the configured database path with -DatabasePath."
}

$files = @(
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
    'app\static\sw.js'
)
$newFiles = @(
    'app\api\broadcast_planner.py',
    'app\engine\broadcast_plan_policy.py',
    'app\services\broadcast_planner.py'
)

foreach ($relative in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $relative) -PathType Leaf)) {
        throw "Updated source file is missing: $relative"
    }
    if ($relative -notin $newFiles -and -not (Test-Path -LiteralPath (Join-Path $resolvedLiveRoot $relative) -PathType Leaf)) {
        throw "Live rollback file is missing: $relative"
    }
}

$service = Get-Service -Name $ServiceName -ErrorAction Stop
$wasRunning = $service.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running
$stamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
$backupRoot = Join-Path $resolvedWorkspaceRoot "deployment-backups\ui-only-$stamp"
$databaseBackupRoot = Join-Path $backupRoot 'database'
$databaseFiles = @(
    $databasePathResolved,
    "$databasePathResolved-wal",
    "$databasePathResolved-shm"
)
$databaseSnapshotCreated = $false
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$serviceStopped = $false

function Wait-ServiceState([string]$Name, [string]$State, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $current = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($current -and [string]$current.Status -eq $State) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Service $Name did not reach $State within $TimeoutSeconds seconds."
}

try {
    foreach ($relative in $files) {
        $liveFile = Join-Path $resolvedLiveRoot $relative
        $backupFile = Join-Path $backupRoot $relative
        if (Test-Path -LiteralPath $liveFile -PathType Leaf) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backupFile) -Force | Out-Null
            Copy-Item -LiteralPath $liveFile -Destination $backupFile -Force
        }
    }

    if ($wasRunning) {
        Stop-Service -Name $ServiceName -Force
        $serviceStopped = $true
        Wait-ServiceState $ServiceName 'Stopped' 60
    }

    New-Item -ItemType Directory -Path $databaseBackupRoot -Force | Out-Null
    foreach ($databaseFile in $databaseFiles) {
        if (Test-Path -LiteralPath $databaseFile -PathType Leaf) {
            Copy-Item -LiteralPath $databaseFile -Destination (Join-Path $databaseBackupRoot (Split-Path -Leaf $databaseFile)) -Force
        }
    }
    $databaseCopy = Join-Path $databaseBackupRoot (Split-Path -Leaf $databasePathResolved)
    if (-not (Test-Path -LiteralPath $databaseCopy -PathType Leaf)) {
        throw 'The pre-deployment database snapshot was not created.'
    }
    $sourceHash = (Get-FileHash -LiteralPath $databasePathResolved -Algorithm SHA256).Hash
    $backupHash = (Get-FileHash -LiteralPath $databaseCopy -Algorithm SHA256).Hash
    if ($sourceHash -ne $backupHash) { throw 'The pre-deployment database snapshot did not match its source.' }
    $databaseSnapshotCreated = $true

    foreach ($relative in $files) {
        $sourceFile = Join-Path $sourceRoot $relative
        $liveFile = Join-Path $resolvedLiveRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $liveFile) -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $liveFile -Force
    }

    if ($wasRunning) {
        Start-Service -Name $ServiceName
        Wait-ServiceState $ServiceName 'Running' 60
        $serviceStopped = $false
    }

    [ordered]@{
        deployed = $true
        files_copied = $files.Count
        service = $ServiceName
        service_state = (Get-Service -Name $ServiceName).Status.ToString()
        rollback_files = $backupRoot
        database_snapshot = $databaseBackupRoot
        verification = 'Complete in the RadioTEDU OnAir UI; this script runs no application tests.'
    } | ConvertTo-Json -Depth 4
} catch {
    $current = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($current -and $current.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        Wait-ServiceState $ServiceName 'Stopped' 60
        $serviceStopped = $true
    }

    if ($databaseSnapshotCreated) {
        foreach ($databaseFile in $databaseFiles) {
            $databaseBackup = Join-Path $databaseBackupRoot (Split-Path -Leaf $databaseFile)
            if (Test-Path -LiteralPath $databaseBackup -PathType Leaf) {
                Copy-Item -LiteralPath $databaseBackup -Destination $databaseFile -Force
            } elseif (Test-Path -LiteralPath $databaseFile -PathType Leaf) {
                Remove-Item -LiteralPath $databaseFile -Force
            }
        }
    }

    foreach ($relative in $files) {
        $backupFile = Join-Path $backupRoot $relative
        $liveFile = Join-Path $resolvedLiveRoot $relative
        if (Test-Path -LiteralPath $backupFile -PathType Leaf) {
            Copy-Item -LiteralPath $backupFile -Destination $liveFile -Force
        } elseif ($relative -in $newFiles -and (Test-Path -LiteralPath $liveFile -PathType Leaf)) {
            Remove-Item -LiteralPath $liveFile -Force
        }
    }

    if ($wasRunning -and $serviceStopped) {
        Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        Wait-ServiceState $ServiceName 'Running' 60
    }
    throw
}
