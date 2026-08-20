[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$DisableQualityOutputs,
    [switch]$SkipStart,
    [int]$StartupTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = 'C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe'
$ServiceHost = 'C:\Program Files\RadioTEDU\OnAir\service-host\RadioTEDU-OnAir-ServiceHost.exe'
$ProgramDataRoot = 'C:\ProgramData\RadioTEDU\OnAir'
$ServicesRoot = Join-Path $ProgramDataRoot 'services'
$MainService = 'RadioTEDU.OnAir.Supervisor'
$AiService = 'RadioTEDU.AIStreams'
$WatchdogTask = 'RadioTEDU OnAir - Audio Watchdog'
$OldMetadataTask = 'RadioTEDU OnAir Metadata Refresh'
$OldCommonStartup = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\RadioTEDU OnAir.lnk'
$DisabledStartup = 'C:\Users\tedu\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Disabled by Codex - old RadioTEDU broadcasters'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this one-shot installer from an elevated PowerShell session.'
    }
}

function Invoke-Sc([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments) {
    & sc.exe @Arguments | Out-Null
    if ($LASTEXITCODE -notin @(0, 1060, 1062)) {
        throw "sc.exe failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
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

function Wait-ProcessCommandLine([string]$Pattern, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $match = Get-CimInstance Win32_Process | Where-Object {
            ([string]$_.CommandLine) -match $Pattern
        } | Select-Object -First 1
        if ($match) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "No managed child matched $Pattern within $TimeoutSeconds seconds."
}

function Wait-OnAirReady([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18110/api/health/ready' -TimeoutSec 3
            if ([int]$response.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "RadioTEDU OnAir did not become ready within $TimeoutSeconds seconds."
}

function Install-ServiceHostService(
    [string]$Name,
    [string]$DisplayName,
    [string]$Description,
    [string]$Config
) {
    $binaryPath = '"{0}" --service-name "{1}" --config "{2}"' -f $ServiceHost, $Name, $Config
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-Service -Name $Name -BinaryPathName $binaryPath -DisplayName $DisplayName `
            -Description $Description -StartupType Automatic | Out-Null
    } else {
        $serviceRecord = Get-CimInstance Win32_Service -Filter ("Name='{0}'" -f $Name.Replace("'", "''"))
        $change = Invoke-CimMethod -InputObject $serviceRecord -MethodName Change -Arguments @{
            PathName = $binaryPath
            DisplayName = $DisplayName
            StartMode = 'Automatic'
        }
        if ([int]$change.ReturnValue -ne 0) {
            throw "Win32_Service.Change failed for $Name with code $($change.ReturnValue)."
        }
    }
    Invoke-Sc description $Name $Description
    Invoke-Sc config $Name 'depend=' 'Tcpip/Dnscache'
    Invoke-Sc failure $Name 'reset=' '600' 'actions=' 'restart/5000/restart/15000/restart/60000'
    Invoke-Sc failureflag $Name 1
}

function Stop-SourceProcesses {
    foreach ($name in @($MainService, $AiService)) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne 'Stopped') {
            Invoke-Sc stop $name
            Wait-ServiceState $name 'Stopped' 60
        }
    }
    $needles = @(
        'app.station_worker_process',
        'run_radio_backend_service.py',
        'run_ai_stream_supervisor.py',
        'run_ai_quality_supervisor.py'
    )
    Get-CimInstance Win32_Process | Where-Object {
        $command = [string]$_.CommandLine
        $needles | Where-Object { $command.Contains($_) }
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Disable-OldOnAirStartup {
    if (Get-ScheduledTask -TaskName $OldMetadataTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $OldMetadataTask -Confirm:$false
    }
    if (Test-Path -LiteralPath $OldCommonStartup -PathType Leaf) {
        New-Item -ItemType Directory -Path $DisabledStartup -Force | Out-Null
        $target = Join-Path $DisabledStartup 'RadioTEDU OnAir - old installed agent.lnk'
        Move-Item -LiteralPath $OldCommonStartup -Destination $target -Force
    }
    Get-CimInstance Win32_Process | Where-Object {
        [string]$_.ExecutablePath -eq 'C:\Program Files\RadioTEDU\OnAir\RadioTEDU-OnAir-Agent.exe'
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Write-ServiceDefinitions {
    New-Item -ItemType Directory -Path $ServicesRoot -Force | Out-Null
    $mainConfig = Join-Path $ServicesRoot 'RadioTEDU.OnAir.Supervisor.services'
    $mainDefinition = @(
        '# name|executable|arguments|working directory|restart'
        ('RadioTEDU-OnAir-Backend|{0}|-u "{1}"|{2}|true' -f $Python, (Join-Path $RepositoryRoot 'tools\run_radio_backend_service.py'), $RepositoryRoot)
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($mainConfig, $mainDefinition + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

    $aiConfig = Join-Path $ServicesRoot 'RadioTEDU.AIStreams.services'
    $aiJson = Join-Path $ServicesRoot 'RadioTEDU.AIStreams.json'
    if (-not (Test-Path -LiteralPath $aiJson -PathType Leaf)) {
        throw "AI supervisor configuration is missing: $aiJson"
    }
    $aiPayload = Get-Content -LiteralPath $aiJson -Raw | ConvertFrom-Json
    $aiPayload | Add-Member -NotePropertyName quality_outputs_path -NotePropertyValue (Join-Path $RepositoryRoot 'run\new-program\data\integrations\quality-outputs.json') -Force
    $aiJsonText = $aiPayload | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($aiJson, $aiJsonText + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    $aiDefinition = @(
        '# name|executable|arguments|working directory|restart'
        ('RadioTEDU-AI-Streams|{0}|-u "C:\RadioTEDU\scripts\run_ai_quality_supervisor.py" --config "{1}"|C:\RadioTEDU|true' -f $Python, $aiJson)
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($aiConfig, $aiDefinition + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    return @{ Main = $mainConfig; Ai = $aiConfig }
}

function Set-ProtectedConfigurationAcl {
    foreach ($path in @(
        (Join-Path $ServicesRoot 'RadioTEDU.OnAir.Supervisor.services'),
        (Join-Path $ServicesRoot 'RadioTEDU.AIStreams.services'),
        (Join-Path $ServicesRoot 'RadioTEDU.AIStreams.json'),
        (Join-Path $ProgramDataRoot 'secrets\station-credentials.json')
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required protected file is missing: $path" }
        & icacls.exe $path /inheritance:r /grant:r 'SYSTEM:(F)' 'Administrators:(F)' 'RADIOTEDUYAYIN\tedu:(F)' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to protect $path" }
    }
}

function Install-Watchdog {
    # This is the same task contract owned by installer\InstallAudioWatchdog.ps1,
    # pointed at the commissioned source runtime on port 18110.
    $watchdog = Join-Path $RepositoryRoot 'tools\RadioTEDU-AudioWatchdog.ps1'
    $ffmpegCandidates = @(
        Get-ChildItem -LiteralPath (Join-Path $RepositoryRoot 'dist') -Directory -Filter 'backend-*' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { Join-Path $_.FullName 'tools\bin\ffmpeg.exe' }
    )
    $ffmpegCandidates += Join-Path $RepositoryRoot 'dist\backend\tools\bin\ffmpeg.exe'
    $ffmpeg = @($ffmpegCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1)[0]
    if (-not $ffmpeg) { throw 'No managed FFmpeg runtime is available for the watchdog.' }
    foreach ($path in @($watchdog, $ffmpeg)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Watchdog dependency is missing: $path"
        }
    }
    $logRoot = 'H:\Broadcast\RadioTEDU-OnAir\Playlists\_state\watchdog'
    if (-not (Test-Path -LiteralPath 'H:\' -PathType Container)) {
        $logRoot = Join-Path $ProgramDataRoot 'Logs\Watchdog'
    }
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $arguments = @(
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden'
        ('-File "{0}"' -f $watchdog)
        '-BackendPort 18110'
        ('-DataRoot "{0}"' -f (Join-Path $RepositoryRoot 'run\new-program\data'))
        ('-FFmpegPath "{0}"' -f $ffmpeg)
        ('-LogRoot "{0}"' -f $logRoot)
        ('-BackendLauncher "{0}"' -f (Join-Path $RepositoryRoot 'tools\start_radio_backend.ps1'))
        ('-SupervisorServiceName "{0}"' -f $MainService)
        ('-AIStreamsServiceName "{0}"' -f $AiService)
    ) -join ' '
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $arguments
    $periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval ([TimeSpan]::FromMinutes(5)) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
    $startupTrigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::FromMinutes(8))
    Register-ScheduledTask -TaskName $WatchdogTask -Action $action `
        -Trigger @($periodicTrigger, $startupTrigger) `
        -Principal $principal -Settings $settings `
        -Description 'Continuity monitor and bounded repair for all RadioTEDU public streams.' `
        -Force -ErrorAction Stop | Out-Null
}

function Invoke-QualityCommissioning {
    $arguments = @((Join-Path $RepositoryRoot 'tools\commission_quality_outputs.py'))
    if ($DisableQualityOutputs) { $arguments += '--disabled' }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Quality-output commissioning failed.' }
}

function Protect-MachineCredentialVault {
    # Rewrap the shared source credential with machine DPAPI so LocalSystem can
    # recover every source after boot. The migration tool never prints secrets.
    $migration = Join-Path $RepositoryRoot 'tools\migrate_credential_vault_scope.py'
    & $Python $migration --scope machine --apply
    if ($LASTEXITCODE -ne 0) { throw 'Machine-DPAPI credential migration failed.' }
}

function Start-DurableServices {
    foreach ($name in @('RadioTEDU.SharedAI', 'RadioTEDUVotingRadio', 'RadioTEDU.JukeLocalMediaAgent')) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
            Invoke-Sc config $name 'start=' 'delayed-auto'
            Invoke-Sc failure $name 'reset=' '600' 'actions=' 'restart/5000/restart/15000/restart/60000'
            $service = Get-Service -Name $name
            if ($service.Status -ne 'Running') { Invoke-Sc start $name }
            Wait-ServiceState $name 'Running' $StartupTimeoutSeconds
        }
    }
    Invoke-Sc start $MainService
    Wait-ServiceState $MainService 'Running' $StartupTimeoutSeconds
    Wait-OnAirReady $StartupTimeoutSeconds
    Invoke-Sc start $AiService
    Wait-ServiceState $AiService 'Running' $StartupTimeoutSeconds
    Wait-ProcessCommandLine 'run_ai_quality_supervisor\.py' $StartupTimeoutSeconds
    schtasks.exe /Change /TN $WatchdogTask /ENABLE | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to enable the audio watchdog task.' }
}

Assert-Administrator
foreach ($required in @($RepositoryRoot, $Python, $ServiceHost, 'C:\RadioTEDU\scripts\run_ai_quality_supervisor.py')) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required installation input is missing: $required" }
}

if ($PSCmdlet.ShouldProcess('RadioTEDU OnAir', 'install/repair durable services and startup ownership')) {
    schtasks.exe /Change /TN $WatchdogTask /DISABLE 2>$null | Out-Null
    Stop-SourceProcesses
    Disable-OldOnAirStartup
    $definitions = Write-ServiceDefinitions
    Protect-MachineCredentialVault
    Set-ProtectedConfigurationAcl
    Install-ServiceHostService $MainService 'RadioTEDU OnAir Supervisor' 'Owns the RadioTEDU OnAir backend and process-isolated station workers.' $definitions.Main
    Install-ServiceHostService $AiService 'RadioTEDU AI Streams' 'Owns the provisioned RadioTEDU English and French legacy streams; unprovisioned quality mounts stay disabled.' $definitions.Ai
    Invoke-QualityCommissioning
    Install-Watchdog
    if (-not $SkipStart) { Start-DurableServices }
}

$result = [ordered]@{
    ok = $true
    repository = $RepositoryRoot
    quality_outputs_enabled = -not [bool]$DisableQualityOutputs
    old_onair_startup_removed = -not (Test-Path -LiteralPath $OldCommonStartup)
    main_service = [string](Get-Service -Name $MainService -ErrorAction SilentlyContinue).Status
    ai_service = [string](Get-Service -Name $AiService -ErrorAction SilentlyContinue).Status
    watchdog = [string](Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue).State
    completed_at = (Get-Date).ToUniversalTime().ToString('o')
}
$resultJson = $result | ConvertTo-Json
$statePath = Join-Path $ProgramDataRoot 'one-shot-install-state.json'
$temporaryState = "$statePath.$([Guid]::NewGuid().ToString('N')).tmp"
[IO.File]::WriteAllText($temporaryState, $resultJson + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryState -Destination $statePath -Force
$resultJson
