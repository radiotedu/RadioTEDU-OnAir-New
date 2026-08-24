[CmdletBinding()]
param(
    [int]$BackendPort = 8100,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$FFmpegPath,
    [string]$LogRoot = "H:\Broadcast\RadioTEDU-OnAir\Playlists\_state\watchdog",
    [string]$ListenerBase = "http://stream.radiotedu.com:11154",
    [string]$BackendLauncher = "",
    [string]$SupervisorServiceName = "RadioTEDU.OnAir.Supervisor",
    [string]$AIStreamsServiceName = "RadioTEDU.AIStreams",
    [ValidateRange(1, 14)][int]$MaxConcurrentAudioProbes = 4,
    [ValidateRange(2.0, 30.0)][double]$TransportFreshnessSeconds = 5.0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$apiRoot = "http://127.0.0.1:$BackendPort"
$script:WatchdogToken = ""
$tokenPath = Join-Path $DataRoot "secrets\watchdog-api.key"
$stateRoot = Join-Path $DataRoot "watchdog"
$repairStatePath = Join-Path $stateRoot "repair-state.json"
$aiRepairStatePath = Join-Path $stateRoot "ai-repair-state.json"
$listenerRoot = $ListenerBase.TrimEnd("/")
$mounts = @(
    [pscustomobject]@{ StationId = 1; Genre = "classical"; Url = "$listenerRoot/classic" },
    [pscustomobject]@{ StationId = 1; Genre = "classical-low"; Url = "$listenerRoot/classic-low" },
    [pscustomobject]@{ StationId = 1; Genre = "classical-flac"; Url = "$listenerRoot/classic-flac" },
    [pscustomobject]@{ StationId = 2; Genre = "lofi"; Url = "$listenerRoot/lofi" },
    [pscustomobject]@{ StationId = 2; Genre = "lofi-low"; Url = "$listenerRoot/lofi-low" },
    [pscustomobject]@{ StationId = 5; Genre = "jazz"; Url = "$listenerRoot/cazz" },
    [pscustomobject]@{ StationId = 5; Genre = "jazz-low"; Url = "$listenerRoot/cazz-low" },
    [pscustomobject]@{ StationId = 5; Genre = "jazz-flac"; Url = "$listenerRoot/cazz-flac" },
    [pscustomobject]@{ StationId = 9; Genre = "energize"; Url = "$listenerRoot/energize" },
    [pscustomobject]@{ StationId = 9; Genre = "energize-low"; Url = "$listenerRoot/energize-low" },
    [pscustomobject]@{ StationId = 4; Genre = "pop"; Url = "$listenerRoot/radio" },
    [pscustomobject]@{ StationId = 4; Genre = "pop-low"; Url = "$listenerRoot/radio-low" },
    [pscustomobject]@{ StationId = 8; Genre = "rock"; Url = "$listenerRoot/rock" },
    [pscustomobject]@{ StationId = 8; Genre = "rock-low"; Url = "$listenerRoot/rock-low" }
)
# English/French AI radio is hosted by the separate Services computer.  This
# streaming PC intentionally owns and monitors only the 14 music mounts.
$auxiliaryMounts = @()

if (-not (Test-Path -LiteralPath $FFmpegPath -PathType Leaf)) {
    throw "Bundled FFmpeg is missing: $FFmpegPath"
}
try {
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
}
catch {
    $LogRoot = Join-Path $DataRoot "Logs\Watchdog"
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
}
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
$logPath = Join-Path $LogRoot "watchdog.log"

function Write-WatchdogLog([string]$Message) {
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value (
        "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message
    )
}

function Test-BackendReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$apiRoot/api/health/live" -TimeoutSec 3
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-BackendPortOpen {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync("127.0.0.1", $BackendPort)
        return $connect.Wait(1000) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-OriginResponsive {
    # A global TinyIce control-plane hang makes every mount probe fail. In that
    # case restarting healthy local sources only creates reconnect churn and can
    # prolong the outage, so require an actual HTTP response before repairs.
    $request = [System.Net.HttpWebRequest]::Create("$listenerRoot/")
    $request.Method = "GET"
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    try {
        $response = $request.GetResponse()
        $response.Close()
        return $true
    }
    catch [System.Net.WebException] {
        if ($null -ne $_.Exception.Response) {
            $_.Exception.Response.Close()
            return $true
        }
        return $false
    }
    catch {
        return $false
    }
}

function Start-BackendIfNeeded {
    if (Test-BackendReady) {
        return
    }
    if (Test-BackendPortOpen) {
        # A temporarily busy backend must not be mistaken for an absent one.
        # Launching a second backend against the same port and station leases
        # creates source-owner churn and audible reconnects.
        $healthDeadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Seconds 1
            if (Test-BackendReady) {
                return
            }
        } while ((Get-Date) -lt $healthDeadline)
        throw "Backend port is owned but the health endpoint remained unavailable; duplicate launch refused."
    }
    $service = Get-Service -Name $SupervisorServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service) {
        if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Running) {
            Start-Service -Name $SupervisorServiceName
        }
    }
    elseif ($BackendLauncher -and (Test-Path -LiteralPath $BackendLauncher -PathType Leaf)) {
        & $BackendLauncher -Port $BackendPort | Out-Null
    }
    else {
        throw "Backend is unavailable and no recovery launcher or supervisor service exists."
    }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        if (Test-BackendReady) {
            return
        }
    } while ((Get-Date) -lt $deadline)
    throw "Backend did not become ready within 60 seconds."
}

function Get-WatchdogToken {
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$apiRoot/api/watchdog/status" -TimeoutSec 5 | Out-Null
        }
        catch {
            # The expected 401 creates the token without disclosing it.
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    do {
        if (Test-Path -LiteralPath $tokenPath -PathType Leaf) {
            $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
            if ($token.Length -ge 32) {
                return $token
            }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Watchdog API token is unavailable."
}

function Invoke-WatchdogApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body = $null
    )
    $headers = @{ "X-RadioTEDU-Watchdog-Token" = $script:WatchdogToken }
    if ($Method -eq "GET") {
        return Invoke-RestMethod -Method Get -Uri ($apiRoot + $Path) -Headers $headers -TimeoutSec 45
    }
    $json = if ($null -eq $Body) { "{}" } else { $Body | ConvertTo-Json -Depth 8 -Compress }
    return Invoke-RestMethod -Method Post -Uri ($apiRoot + $Path) -Headers $headers `
        -ContentType "application/json" -Body $json -TimeoutSec 180
}

function Test-ManagedProfilesHealthy([object]$Snapshot) {
    if ($null -eq $Snapshot) {
        return $false
    }
    if ([bool]$Snapshot.managed_profiles_ok) {
        return $true
    }
    # Compatibility during a no-disconnect backend rollout: the previous
    # backend expected campaign folders to be non-recursive. The durable live
    # folder policy is recursive, so independently validate every other health
    # field and accept recursive=true until the backend next starts from the
    # updated source.
    $profiles = @($Snapshot.managed_profiles)
    if ($profiles.Count -eq 0) {
        return $false
    }
    foreach ($profile in $profiles) {
        if (
            -not [bool]$profile.folder_exists -or
            -not [bool]$profile.folder_matches -or
            -not [bool]$profile.replace_mode -or
            [int]$profile.rescan_interval_seconds -ne 600 -or
            -not [bool]$profile.recursive -or
            [int]$profile.active_tracks -le 0
        ) {
            return $false
        }
    }
    return $true
}

function Start-PublicAudioProbe([pscustomobject]$Mount) {
    $arguments = @(
        "-hide_banner", "-nostdin", "-loglevel", "info",
        "-re", "-stats_period", "8", "-rw_timeout", "12000000",
        "-i", ('"' + $Mount.Url + '"'), "-t", "8", "-af", "volumedetect", "-f", "null", "NUL"
    ) -join " "
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FFmpegPath
    $startInfo.Arguments = $arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $null = $process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    return [pscustomobject]@{
        Mount = $Mount
        Process = $process
        StdoutTask = $stdoutTask
        StderrTask = $stderrTask
    }
}

function Complete-PublicAudioProbe([pscustomobject]$Probe, [datetime]$Deadline) {
    $process = $Probe.Process
    $mount = $Probe.Mount
    $remainingMs = [math]::Max(1, [int]($Deadline - (Get-Date)).TotalMilliseconds)
    if (-not $process.WaitForExit($remainingMs)) {
        try { $process.Kill() } catch {}
        $process.WaitForExit()
        return [pscustomobject]@{
            station_id = $mount.StationId; genre = $mount.Genre; decoded = $false;
            audible = $false; media_seconds = 0.0; mean_db = $null; max_db = $null;
            reason = "timeout"
        }
    }
    $output = $Probe.StdoutTask.Result + "`n" + $Probe.StderrTask.Result
    $meanMatch = [regex]::Match($output, "mean_volume:\s*(-?[0-9.]+)\s*dB")
    $maxMatch = [regex]::Match($output, "max_volume:\s*(-?[0-9.]+)\s*dB")
    $timeMatches = [regex]::Matches($output, "time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    $meanDb = if ($meanMatch.Success) { [double]$meanMatch.Groups[1].Value } else { $null }
    $maxDb = if ($maxMatch.Success) { [double]$maxMatch.Groups[1].Value } else { $null }
    $mediaSeconds = 0.0
    if ($timeMatches.Count -gt 0) {
        $groups = $timeMatches[$timeMatches.Count - 1].Groups
        $mediaSeconds = ([double]$groups[1].Value * 3600.0) +
            ([double]$groups[2].Value * 60.0) + [double]$groups[3].Value
    }
    $decoded = $process.ExitCode -eq 0 -and $null -ne $meanDb -and
        $null -ne $maxDb -and $mediaSeconds -ge 7.5
    $audible = $decoded -and $meanDb -gt -65.0 -and $maxDb -gt -50.0
    return [pscustomobject]@{
        station_id = $mount.StationId; genre = $mount.Genre; decoded = $decoded;
        audible = $audible; media_seconds = [math]::Round($mediaSeconds, 3);
        mean_db = $meanDb; max_db = $maxDb;
        reason = if ($audible) { "ok" } elseif ($decoded) { "silent" }
            elseif ($mediaSeconds -gt 0.0) { "short_read" } else { "decode_failed" }
    }
}

function Test-PublicAudio([pscustomobject]$Mount) {
    return Complete-PublicAudioProbe (Start-PublicAudioProbe $Mount) ((Get-Date).AddSeconds(25))
}

function Invoke-PublicAudioProbeBatches([object[]]$Selected) {
    $items = @($Selected)
    $itemCount = @($items).Count
    $rows = @()
    for ($offset = 0; $offset -lt $itemCount; $offset += $MaxConcurrentAudioProbes) {
        $last = [math]::Min(
            $itemCount - 1,
            $offset + $MaxConcurrentAudioProbes - 1
        )
        $batchSize = ($last - $offset) + 1
        $batch = @($items | Select-Object -Skip $offset -First $batchSize)
        $probes = @($batch | ForEach-Object { Start-PublicAudioProbe $_ })
        $deadline = (Get-Date).AddSeconds(25)
        $rows += @(
            $probes | ForEach-Object { Complete-PublicAudioProbe $_ $deadline }
        )
    }
    return @($rows)
}

function Test-SelectedStreams([int[]]$StationIds) {
    $selected = if ($StationIds.Count -gt 0) {
        @($mounts | Where-Object { $StationIds -contains [int]$_.StationId })
    }
    else {
        @($mounts)
    }
    return @(Invoke-PublicAudioProbeBatches $selected)
}

function Test-SelectedAuxiliaryStreams([int[]]$StationIds) {
    $selected = if ($StationIds.Count -gt 0) {
        @($auxiliaryMounts | Where-Object { $StationIds -contains [int]$_.StationId })
    }
    else {
        @($auxiliaryMounts)
    }
    return @(Invoke-PublicAudioProbeBatches $selected)
}

function Test-AIRepairCooldown {
    if (-not (Test-Path -LiteralPath $aiRepairStatePath -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $aiRepairStatePath -Raw | ConvertFrom-Json
        $repairedAt = [datetime]::Parse([string]$state.repaired_at).ToUniversalTime()
        return ((Get-Date).ToUniversalTime() - $repairedAt).TotalMinutes -lt 15
    }
    catch {
        return $false
    }
}

function Save-AIRepairState([string]$Reason) {
    [ordered]@{
        repaired_at = (Get-Date).ToUniversalTime().ToString("o")
        reason = $Reason
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $aiRepairStatePath -Encoding UTF8
}

function Repair-AuxiliaryStreams([int[]]$FailedIds) {
    if ($FailedIds.Count -eq 0) {
        return [pscustomobject]@{ failed_ids = @(); repaired = $false; cooldown = $false }
    }
    if (Test-AIRepairCooldown) {
        Write-WatchdogLog (
            "Confirmed AI stream failure suppressed by 15-minute repair cooldown ids=" +
            ($FailedIds -join ",")
        )
        return [pscustomobject]@{ failed_ids = @($FailedIds); repaired = $false; cooldown = $true }
    }
    $service = Get-Service -Name $AIStreamsServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        Write-WatchdogLog "AI stream service is unavailable; automatic recovery cannot run."
        return [pscustomobject]@{ failed_ids = @($FailedIds); repaired = $false; cooldown = $false }
    }
    Restart-Service -Name $AIStreamsServiceName -Force
    Save-AIRepairState ("mounts=" + ($FailedIds -join ","))
    Start-Sleep -Seconds 8
    $verification = Test-SelectedAuxiliaryStreams $FailedIds
    $stillFailed = @(
        $verification |
            Where-Object { -not ($_.decoded -and $_.audible) } |
            ForEach-Object { [int]$_.station_id }
    )
    Write-WatchdogLog (
        "AI stream service recovery completed remaining_failed=" + ($stillFailed -join ",")
    )
    return [pscustomobject]@{
        failed_ids = @($stillFailed); repaired = $true; cooldown = $false
    }
}

function Get-OptionalProperty(
    [object]$Object,
    [string]$Name,
    [object]$Default
) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) {
        return $Object.PSObject.Properties[$Name].Value
    }
    return $Default
}

function Get-LocalTransportState([int]$StationId) {
    $heartbeatPath = Join-Path $DataRoot (
        "State\StationWorkers\station-{0}.heartbeat.json" -f $StationId
    )
    if (-not (Test-Path -LiteralPath $heartbeatPath -PathType Leaf)) {
        return [pscustomobject]@{
            station_id = $StationId; healthy = $false; reason = "heartbeat_missing"
        }
    }
    try {
        $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
        $updatedEpoch = [double](Get-OptionalProperty $heartbeat "updated_epoch" 0.0)
        $updatedAt = [DateTimeOffset]::FromUnixTimeMilliseconds(
            [int64]($updatedEpoch * 1000.0)
        )
        $heartbeatAge = (
            [DateTimeOffset]::UtcNow - $updatedAt
        ).TotalSeconds
        $runtime = Get-OptionalProperty $heartbeat "runtime_status" $null
        $mount = Get-OptionalProperty $runtime "icecast_mount_health" $null
        $pcmAge = [double](Get-OptionalProperty $runtime "program_pcm_age_seconds" 999999.0)
        $lastWriteAge = [double](Get-OptionalProperty $mount "last_write_age_seconds" 999999.0)
        $healthy = [bool](Get-OptionalProperty $heartbeat "running" $false) -and
            $heartbeatAge -le 90.0 -and
            [bool](Get-OptionalProperty $runtime "running" $false) -and
            [bool](Get-OptionalProperty $runtime "program_running" $false) -and
            -not [bool](Get-OptionalProperty $runtime "program_pcm_stalled" $false) -and
            $pcmAge -le $TransportFreshnessSeconds -and
            [bool](Get-OptionalProperty $runtime "icecast_sink_running" $false) -and
            [bool](Get-OptionalProperty $mount "process_running" $false) -and
            [bool](Get-OptionalProperty $mount "writer_running" $false) -and
            -not [bool](Get-OptionalProperty $mount "writer_failed" $false) -and
            -not [bool](Get-OptionalProperty $mount "writer_backpressured" $false) -and
            $lastWriteAge -le $TransportFreshnessSeconds
        return [pscustomobject]@{
            station_id = $StationId
            healthy = $healthy
            reason = if ($healthy) { "local_transport_flowing" } else { "local_transport_unhealthy" }
        }
    }
    catch {
        return [pscustomobject]@{
            station_id = $StationId; healthy = $false; reason = "heartbeat_invalid"
        }
    }
}

function Get-RepairableStationIds([int[]]$FailedIds) {
    $repairable = @()
    foreach ($stationId in $FailedIds) {
        $transport = Get-LocalTransportState ([int]$stationId)
        if (-not [bool]$transport.healthy) {
            $repairable += [int]$stationId
        }
    }
    return @($repairable)
}

function Test-RepairCooldown {
    if (-not (Test-Path -LiteralPath $repairStatePath -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $repairStatePath -Raw | ConvertFrom-Json
        $repairedAt = [datetime]::Parse([string]$state.repaired_at).ToUniversalTime()
        return ((Get-Date).ToUniversalTime() - $repairedAt).TotalMinutes -lt 15
    }
    catch {
        return $false
    }
}

function Save-RepairState([string]$Reason) {
    [ordered]@{
        repaired_at = (Get-Date).ToUniversalTime().ToString("o")
        reason = $Reason
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $repairStatePath -Encoding UTF8
}

function Send-Report(
    [string]$Status,
    [string]$Message,
    [int[]]$FailedIds,
    [bool]$ManagedProfilesOk
) {
    try {
        Invoke-WatchdogApi -Method POST -Path "/api/watchdog/report" -Body @{
            status = $Status
            message = $Message
            failed_station_ids = @($FailedIds)
            managed_profiles_ok = $ManagedProfilesOk
        } | Out-Null
    }
    catch {
        Write-WatchdogLog ("Report API failed: " + $_.Exception.Message)
    }
}

try {
    Start-BackendIfNeeded
    $script:WatchdogToken = Get-WatchdogToken
    if (-not (Test-OriginResponsive)) {
        Write-WatchdogLog "TinyIce origin did not return HTTP; waiting 30 seconds for confirmation."
        Start-Sleep -Seconds 30
        if (-not (Test-OriginResponsive)) {
            Send-Report "origin_unavailable" (
                "TinyIce accepted no HTTP response; all local source restarts were suppressed."
            ) @() $true
            Write-WatchdogLog (
                "Origin unavailable after two checks; local source and AI restarts suppressed."
            )
            exit 20
        }
    }
    $firstSnapshot = Invoke-WatchdogApi -Method GET -Path "/api/watchdog/status"
    $firstProfilesHealthy = Test-ManagedProfilesHealthy $firstSnapshot
    $firstAudio = Test-SelectedStreams @()
    $firstAuxiliaryAudio = Test-SelectedAuxiliaryStreams @()
    $firstFailed = @($firstAudio | Where-Object { -not ($_.decoded -and $_.audible) } | ForEach-Object { [int]$_.station_id } | Sort-Object -Unique)
    $firstAuxiliaryFailed = @($firstAuxiliaryAudio | Where-Object { -not ($_.decoded -and $_.audible) } | ForEach-Object { [int]$_.station_id })
    if ($firstFailed.Count -eq 0 -and $firstAuxiliaryFailed.Count -eq 0 -and $firstProfilesHealthy) {
        Send-Report "ok" "All 14 public music mounts decoded as audible and managed profiles were healthy." @() $true
        Write-WatchdogLog "OK: 14 public music mounts audible; managed profiles healthy."
        exit 0
    }

    Write-WatchdogLog (
        "First check failed stations={0} auxiliary={1} managed_profiles_ok={2}; waiting 30 seconds for confirmation." -f
        ($firstFailed -join ","), ($firstAuxiliaryFailed -join ","), $firstProfilesHealthy
    )
    Start-Sleep -Seconds 30
    $secondSnapshot = Invoke-WatchdogApi -Method GET -Path "/api/watchdog/status"
    $secondProfilesHealthy = Test-ManagedProfilesHealthy $secondSnapshot
    $secondAudio = Test-SelectedStreams $firstFailed
    $secondAuxiliaryAudio = Test-SelectedAuxiliaryStreams $firstAuxiliaryFailed
    $secondFailed = @($secondAudio | Where-Object { -not ($_.decoded -and $_.audible) } | ForEach-Object { [int]$_.station_id } | Sort-Object -Unique)
    $secondAuxiliaryFailed = @($secondAuxiliaryAudio | Where-Object { -not ($_.decoded -and $_.audible) } | ForEach-Object { [int]$_.station_id })
    $auxiliaryRecovery = Repair-AuxiliaryStreams $secondAuxiliaryFailed
    $remainingAuxiliaryFailed = @($auxiliaryRecovery.failed_ids)
    $profileRepair = -not $secondProfilesHealthy
    if ($secondFailed.Count -eq 0 -and -not $profileRepair) {
        if ($remainingAuxiliaryFailed.Count -gt 0) {
            Send-Report "failed" "AI stream recovery did not restore every auxiliary mount." @() $true
            Write-WatchdogLog (
                "AI stream recovery unresolved ids=" + ($remainingAuxiliaryFailed -join ",")
            )
            exit 24
        }
        $status = if ([bool]$auxiliaryRecovery.repaired) { "repaired" } else { "transient" }
        $message = if ([bool]$auxiliaryRecovery.repaired) {
            "Confirmed AI stream failure was repaired and verified."
        }
        else {
            "The second check passed; no repair was performed."
        }
        Send-Report $status $message @() $true
        Write-WatchdogLog $message
        exit 0
    }
    # A source can keep accepting local PCM while its public mount has vanished
    # or stopped serving listeners.  After two failed public probes and a
    # responsive origin, re-register every affected station even when the local
    # transport heartbeat still looks healthy.  The repair cooldown below
    # prevents source-owner churn; a global origin outage was already excluded
    # by Test-OriginResponsive.
    $locallyUnhealthyFailed = @(Get-RepairableStationIds $secondFailed)
    $publicOnlyFailed = @(
        $secondFailed | Where-Object { $locallyUnhealthyFailed -notcontains [int]$_ }
    )
    $repairableFailed = @($secondFailed)
    if (Test-RepairCooldown) {
        Send-Report "cooldown" "Confirmed public audio failure, but the 15-minute successful-repair cooldown prevented a loop." $repairableFailed (-not $profileRepair)
        Write-WatchdogLog "Confirmed public audio failure suppressed by 15-minute successful-repair cooldown."
        exit 21
    }

    if ($publicOnlyFailed.Count -gt 0) {
        Write-WatchdogLog (
            "Confirmed public-only failure stations={0}; forcing source re-registration." -f
            ($publicOnlyFailed -join ",")
        )
    }

    $repair = Invoke-WatchdogApi -Method POST -Path "/api/watchdog/repair" -Body @{
        station_ids = @($repairableFailed)
        repair_managed_profiles = $profileRepair
    }
    if (-not [bool]$repair.ok) {
        throw "Repair API returned an incomplete result."
    }
    Start-Sleep -Seconds 15
    $finalSnapshot = Invoke-WatchdogApi -Method GET -Path "/api/watchdog/status"
    $finalProfilesHealthy = Test-ManagedProfilesHealthy $finalSnapshot
    $finalAudio = Test-SelectedStreams $repairableFailed
    $finalFailed = @($finalAudio | Where-Object { -not ($_.decoded -and $_.audible) } | ForEach-Object { [int]$_.station_id } | Sort-Object -Unique)
    if ($finalFailed.Count -gt 0 -or $remainingAuxiliaryFailed.Count -gt 0 -or -not $finalProfilesHealthy) {
        Send-Report "failed" "Repair completed but final verification still failed." $finalFailed $finalProfilesHealthy
        Write-WatchdogLog (
            "Repair final verification failed stations=" + ($finalFailed -join ",") +
            " auxiliary=" + ($remainingAuxiliaryFailed -join ",")
        )
        exit 22
    }
    # Only successful repairs enter cooldown.  A failed final verification must
    # remain eligible for another attempt on the next scheduled run.
    Save-RepairState ("audio={0};profiles={1}" -f ($repairableFailed -join ","), $profileRepair)
    Send-Report "repaired" "Confirmed failures were repaired and final verification passed." @() $true
    Write-WatchdogLog "Repair and final verification passed."
    exit 0
}
catch {
    $watchdogError = $_
    try {
        if ($script:WatchdogToken) {
            Send-Report "error" $watchdogError.Exception.Message @() $false
        }
    }
    catch {}
    $errorLine = [int]$watchdogError.InvocationInfo.ScriptLineNumber
    $errorTrace = [string]$watchdogError.ScriptStackTrace
    Write-WatchdogLog (
        "WATCHDOG_ERROR line={0}: {1}; stack={2}" -f
        $errorLine, $watchdogError.Exception.Message, $errorTrace
    )
    exit 23
}
