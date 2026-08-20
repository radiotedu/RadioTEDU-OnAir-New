[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Prepare", "Install", "Rollback", "Remove")]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$AppRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [int]$BackendPort = 8100,
    [string]$ServiceName = "RadioTEDU.OnAir.Supervisor",
    [string]$TaskName = "RadioTEDU OnAir - Audio Watchdog"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$stateRoot = Join-Path $DataRoot "State\WatchdogInstaller"
$previousTaskPath = Join-Path $stateRoot "previous-task.xml"
$newTaskMarker = Join-Path $stateRoot "task-was-new.marker"
$watchdogPath = Join-Path $AppRoot "tools\RadioTEDU-AudioWatchdog.ps1"
$ffmpegPath = Join-Path $AppRoot "backend\tools\bin\ffmpeg.exe"

function Remove-InstallerState {
    Remove-Item -LiteralPath $previousTaskPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $newTaskMarker -Force -ErrorAction SilentlyContinue
}

if ($Action -eq "Prepare") {
    New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
    Remove-InstallerState
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $previousTaskPath -Encoding Unicode
    }
    else {
        Set-Content -LiteralPath $newTaskMarker -Value "new" -Encoding ASCII
    }
    return
}

if ($Action -eq "Rollback") {
    if (Test-Path -LiteralPath $previousTaskPath -PathType Leaf) {
        $xml = Get-Content -LiteralPath $previousTaskPath -Raw
        Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
    }
    elseif (Test-Path -LiteralPath $newTaskMarker -PathType Leaf) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    Remove-InstallerState
    return
}

if ($Action -eq "Remove") {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-InstallerState
    return
}

foreach ($required in @($watchdogPath, $ffmpegPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required watchdog dependency is missing: $required"
    }
}
$logRoot = if (Test-Path -LiteralPath "H:\" -PathType Container) {
    "H:\Broadcast\RadioTEDU-OnAir\Playlists\_state\watchdog"
}
else {
    Join-Path $DataRoot "Logs\Watchdog"
}
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -BackendPort {1} ' +
    '-DataRoot "{2}" -FFmpegPath "{3}" -LogRoot "{4}" -SupervisorServiceName "{5}"'
) -f $watchdogPath, $BackendPort, $DataRoot, $ffmpegPath, $logRoot, $ServiceName
$taskAction = New-ScheduledTaskAction -Execute $powerShellPath -Argument $arguments
$periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval ([TimeSpan]::FromMinutes(5)) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(4))
Register-ScheduledTask -TaskName $TaskName -Action $taskAction `
    -Trigger @($periodicTrigger, $startupTrigger) `
    -Principal $principal -Settings $settings -Description (
        "Double-checks decoded public audio, repairs managed playlists, and restarts only failed RadioTEDU stations."
    ) -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Remove-InstallerState
