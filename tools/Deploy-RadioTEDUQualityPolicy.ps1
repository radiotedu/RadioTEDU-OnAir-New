[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$LiveRoot = 'C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio',
    [string]$ServiceName = 'RadioTEDU.OnAir.Supervisor',
    [string]$Database = 'C:\ProgramData\RadioTEDU\OnAir\cleanroom.db',
    [string]$BackupRoot = 'C:\ProgramData\RadioTEDU\OnAir\backups'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ResolvedLiveRoot = (Resolve-Path -LiteralPath $LiveRoot).Path
$Python = 'C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe'
$Files = @(
    'app\api\legacy.py',
    'app\api\setup.py',
    'app\api\stations.py',
    'app\api\stream_config.py',
    'app\api\streaming.py',
    'app\audio\gst_pipeline.py',
    'app\db.py',
    'app\engine\broadcast_queue_autofill.py',
    'app\engine\process_worker_child.py',
    'app\engine\process_worker_manager.py',
    'app\engine\runtime_registry.py',
    'app\main.py',
    'app\repositories\settings_repo.py',
    'app\repositories\station_output_repo.py',
    'app\services\quality_outputs.py',
    'app\services\replication_applier.py',
    'app\services\stream_config_service.py',
    'app\static\js\setup-wizard.js',
    'app\static\onair\app.js',
    'app\static\onair\index.html',
    'app\static\sw.js',
    'tools\commission_quality_outputs.py',
    'tools\RadioTEDU-AudioWatchdog.ps1',
    'tools\verify_quality_commissioning.py',
    'tools\verify_live_runtime.py'
)

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this deployment from an elevated PowerShell session.'
    }
}

function Wait-ServiceState([string]$Name, [string]$State, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service -and [string]$service.Status -eq $State) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Service $Name did not reach $State within $TimeoutSeconds seconds."
}

function Wait-TcpListener([int]$Port, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "No local listener appeared on port $Port within $TimeoutSeconds seconds."
}

Assert-Administrator
foreach ($required in @($SourceRoot, $ResolvedLiveRoot, $Python, $Database)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required deployment input is missing: $required"
    }
}
foreach ($relative in $Files) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $relative) -PathType Leaf)) {
        throw "Source file is missing: $relative"
    }
}

$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$SourceBackup = Join-Path $BackupRoot "source-policy-$timestamp"
$DatabaseBackupRoot = Join-Path $BackupRoot 'quality-commission'
$commissioned = $false
$serviceStopped = $false

if ($PSCmdlet.ShouldProcess($ResolvedLiveRoot, 'deploy approved RadioTEDU 16-mount policy')) {
    try {
        foreach ($relative in $Files) {
            $live = Join-Path $ResolvedLiveRoot $relative
            $backup = Join-Path $SourceBackup $relative
            if (Test-Path -LiteralPath $live -PathType Leaf) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
                Copy-Item -LiteralPath $live -Destination $backup -Force
            }
        }

        Stop-Service -Name $ServiceName -Force
        Wait-ServiceState $ServiceName 'Stopped' 60
        $serviceStopped = $true

        foreach ($relative in $Files) {
            $source = Join-Path $SourceRoot $relative
            $live = Join-Path $ResolvedLiveRoot $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $live) -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination $live -Force
        }

        New-Item -ItemType Directory -Path $DatabaseBackupRoot -Force | Out-Null
        $commissionOutput = & $Python (Join-Path $ResolvedLiveRoot 'tools\commission_quality_outputs.py') `
            --backup-root $DatabaseBackupRoot 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Quality commissioning failed: $($commissionOutput -join [Environment]::NewLine)"
        }
        $commissioned = $true

        Start-Service -Name $ServiceName
        Wait-ServiceState $ServiceName 'Running' 60
        Wait-TcpListener 18110 120
        $serviceStopped = $false

        $verifyOutput = & $Python (Join-Path $ResolvedLiveRoot 'tools\verify_quality_commissioning.py') `
            --database $Database 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Quality verification failed: $($verifyOutput -join [Environment]::NewLine)"
        }

        $runtimeDeadline = (Get-Date).AddSeconds(120)
        $runtimeOutput = @()
        do {
            $runtimeOutput = @(& $Python (Join-Path $ResolvedLiveRoot 'tools\verify_live_runtime.py') 2>$null)
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep -Seconds 2
        } while ((Get-Date) -lt $runtimeDeadline)
        if ($LASTEXITCODE -ne 0) {
            throw "Live station workers did not become ready: $($runtimeOutput -join [Environment]::NewLine)"
        }

        [ordered]@{
            ok = $true
            live_root = $ResolvedLiveRoot
            source_backup = $SourceBackup
            database_backup_root = $DatabaseBackupRoot
            service = $ServiceName
            service_state = (Get-Service -Name $ServiceName).Status.ToString()
            port_18110_listening = $true
            commission = ($commissionOutput -join [Environment]::NewLine | ConvertFrom-Json)
            verification = ($verifyOutput -join [Environment]::NewLine | ConvertFrom-Json)
            live_runtime = ($runtimeOutput -join [Environment]::NewLine | ConvertFrom-Json)
        } | ConvertTo-Json -Depth 12
    } catch {
        if (-not $commissioned) {
            foreach ($relative in $Files) {
                $backup = Join-Path $SourceBackup $relative
                $live = Join-Path $ResolvedLiveRoot $relative
                if (Test-Path -LiteralPath $backup -PathType Leaf) {
                    Copy-Item -LiteralPath $backup -Destination $live -Force
                }
            }
        }
        if ($serviceStopped -and (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
            Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        }
        throw
    }
}
