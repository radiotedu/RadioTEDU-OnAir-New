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
    [ValidateRange(2.0, 30.0)][double]$TransportFreshnessSeconds = 5.0,
    [ValidateRange(2, 10)][int]$PublicFailureEscalationRuns = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$apiRoot = "http://127.0.0.1:$BackendPort"
$script:WatchdogToken = ""
$tokenPath = Join-Path $DataRoot "secrets\watchdog-api.key"
$stateRoot = Join-Path $DataRoot "watchdog"
$repairStatePath = Join-Path $stateRoot "repair-state.json"
$aiRepairStatePath = Join-Path $stateRoot "ai-repair-state.json"
$publicFailureStatePath = Join-Path $stateRoot "public-failure-state.json"
$backendReloadMarkerPath = Join-Path $stateRoot "backend-source-reload.pending"
$backendReloadAppliedPath = Join-Path $stateRoot "backend-source-reload.applied"
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
    [pscustomobject]@{ StationId = 8; Genre = "rock-low"; Url = "$listenerRoot/rock-low" },
    [pscustomobject]@{ StationId = 10; Genre = "situation"; Url = "$listenerRoot/situation" },
    [pscustomobject]@{ StationId = 11; Genre = "maincharacter"; Url = "$listenerRoot/maincharacter" }
)
# English/French AI radio is hosted by the separate Services computer. This
# host checks local source writers for its configured radio station workers.
$auxiliaryMounts = @()

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
    if (Test-BackendPortOpen) {
        return
    }
    # Local worker recovery is independent of backend API readiness. Starting a
    # second process while a listener is already owned can churn source leases.
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
        if (Test-BackendPortOpen) {
            return
        }
    } while ((Get-Date) -lt $deadline)
    Write-WatchdogLog "Backend listener did not become available; continuing local worker checks."
}

function Invoke-PendingBackendSourceReload {
    if (-not (Test-Path -LiteralPath $backendReloadMarkerPath -PathType Leaf)) {
        return
    }
    $listener = @(
        Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )
    if ($listener.Count -ne 1 -or [int]$listener[0].OwningProcess -le 0) {
        throw "Backend reload was requested, but its listener PID is unavailable."
    }
    $oldPid = [int]$listener[0].OwningProcess
    Move-Item -LiteralPath $backendReloadMarkerPath -Destination $backendReloadAppliedPath -Force
    Write-WatchdogLog "Applying one-time backend source reload pid=$oldPid; station workers remain active."
    Stop-Process -Id $oldPid -Force -ErrorAction Stop

    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        $replacement = @(
            Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue |
                Where-Object { [int]$_.OwningProcess -ne $oldPid } |
                Select-Object -First 1
        )
        if ($replacement.Count -eq 1 -and (Test-BackendReady)) {
            Write-WatchdogLog (
                "Backend source reload completed old_pid={0} new_pid={1}." -f
                $oldPid, [int]$replacement[0].OwningProcess
            )
            return
        }
    } while ((Get-Date) -lt $deadline)
    throw "Backend source reload did not become ready within 60 seconds."
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
    Write-WatchdogLog "Watchdog API token is unavailable; continuing local source checks without API reporting."
    return ""
}

function Invoke-WatchdogApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body = $null
    )
    $headers = @{ "X-RadioTEDU-Watchdog-Token" = $script:WatchdogToken }
    if ($Method -eq "GET") {
        return Invoke-RestMethod -Method Get -Uri ($apiRoot + $Path) -Headers $headers -TimeoutSec 8
    }
    $json = if ($null -eq $Body) { "{}" } else { $Body | ConvertTo-Json -Depth 8 -Compress }
    return Invoke-RestMethod -Method Post -Uri ($apiRoot + $Path) -Headers $headers `
        -ContentType "application/json" -Body $json -TimeoutSec 180
}

function Get-WatchdogStatusSnapshot {
    try {
        Write-WatchdogLog "Requesting optional watchdog status snapshot."
        return Invoke-WatchdogApi -Method GET -Path "/api/watchdog/status"
    }
    catch {
        Write-WatchdogLog (
            "Watchdog status API unavailable; continuing local source checks: " +
            $_.Exception.Message
        )
        return $null
    }
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
    # RTSAS keeps ordinary listener sessions until its audio writer observes a
    # failed write.  Give each decoder probe a unique identity so we can remove
    # it explicitly even when a silent/stalled mount never writes another byte.
    $probeUserAgent = "RadioTEDU-AudioWatch/{0}" -f [guid]::NewGuid().ToString("N")
    $arguments = @(
        "-hide_banner", "-nostdin", "-loglevel", "info",
        "-re", "-stats_period", "8", "-rw_timeout", "12000000",
        "-user_agent", ('"' + $probeUserAgent + '"'),
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
        UserAgent = $probeUserAgent
    }
}

function Remove-PublicAudioProbeListener([pscustomobject]$Probe) {
    try {
        $streamUri = [uri]$Probe.Mount.Url
        $authority = "{0}://{1}:{2}" -f $streamUri.Scheme, $streamUri.Host, $streamUri.Port
        $encodedMount = [uri]::EscapeDataString($streamUri.AbsolutePath)
        # Keep the JSON array flat; wrapping the REST call in @() nests it
        # as one pipeline item, so per-listener UserAgent matching never runs.
        $clients = Invoke-RestMethod -Method Get -Uri (
            "$authority/admin/listclients?mount=$encodedMount"
        ) -TimeoutSec 5
        foreach ($client in @($clients | Where-Object {
            [string]$_.UserAgent -eq [string]$Probe.UserAgent
        })) {
            Invoke-WebRequest -UseBasicParsing -Method Get -Uri (
                "$authority/admin/killclient?id=$([long]$client.Id)"
            ) -TimeoutSec 5 | Out-Null
        }
    }
    catch {
        # Standard Icecast origins may require admin authentication or omit
        # these endpoints. Probe cleanup is best-effort and never masks audio.
    }
}

function Complete-PublicAudioProbe([pscustomobject]$Probe, [datetime]$Deadline) {
    $process = $Probe.Process
    $mount = $Probe.Mount
    $remainingMs = [math]::Max(1, [int]($Deadline - (Get-Date)).TotalMilliseconds)
    if (-not $process.WaitForExit($remainingMs)) {
        try { $process.Kill() } catch {}
        $process.WaitForExit()
        Remove-PublicAudioProbeListener $Probe
        return [pscustomobject]@{
            station_id = $mount.StationId; genre = $mount.Genre; decoded = $false;
            mount = ([uri]$mount.Url).AbsolutePath;
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
    Remove-PublicAudioProbeListener $Probe
    return [pscustomobject]@{
        station_id = $mount.StationId; genre = $mount.Genre; decoded = $decoded;
        mount = ([uri]$mount.Url).AbsolutePath;
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
        $lastNetworkWriteAge = [double](
            Get-OptionalProperty $mount "last_network_write_age_seconds" 999999.0
        )
        $queuedPcmSeconds = [double](
            Get-OptionalProperty $mount "queued_pcm_seconds" 0.0
        )
        $queueCapacityChunks = [double](
            Get-OptionalProperty $mount "pcm_queue_capacity_chunks" 0.0
        )
        $queueCapacitySeconds = $queueCapacityChunks * 4096.0 / (48000.0 * 2.0 * 2.0)
        $backpressureAge = [double](
            Get-OptionalProperty $mount "writer_backpressure_age_seconds" 0.0
        )
        $sustainedQueueSaturation = [bool](
            (Get-OptionalProperty $mount "writer_backpressured" $false) -and
            $backpressureAge -ge 30.0 -and
            $queueCapacitySeconds -gt 0.0 -and
            $queuedPcmSeconds -ge ($queueCapacitySeconds * 0.9)
        )
        # Judge the actual writer timestamps below. The aggregate worker flag
        # also includes historical queue pressure and unrelated output branches.
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
            -not [bool](Get-OptionalProperty $mount "network_failed" $false) -and
            -not $sustainedQueueSaturation -and
            $lastWriteAge -le $TransportFreshnessSeconds -and
            $lastNetworkWriteAge -le $TransportFreshnessSeconds
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

function Get-WatchdogStationIds([object]$Snapshot) {
    $snapshotStations = @(Get-OptionalProperty $Snapshot "stations" @())
    $snapshotIds = @(
        $snapshotStations |
            ForEach-Object { [int](Get-OptionalProperty $_ "station_id" 0) }
    )
    $configuredOutputIds = @(
        $mounts | ForEach-Object { [int]$_.StationId }
    )
    return @(
        @($snapshotIds) + @($configuredOutputIds) |
            Where-Object { [int]$_ -gt 0 } |
            Sort-Object -Unique
    )
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

function Read-PublicFailureState {
    $result = @{}
    if (-not (Test-Path -LiteralPath $publicFailureStatePath -PathType Leaf)) {
        return $result
    }
    try {
        $payload = Get-Content -LiteralPath $publicFailureStatePath -Raw | ConvertFrom-Json
        $stations = Get-OptionalProperty $payload "stations" $null
        if ($null -eq $stations) {
            return $result
        }
        foreach ($property in $stations.PSObject.Properties) {
            $entry = $property.Value
            $result[[string]$property.Name] = [ordered]@{
                count = [int](Get-OptionalProperty $entry "count" 0)
                first_failed_at = [string](Get-OptionalProperty $entry "first_failed_at" "")
                last_failed_at = [string](Get-OptionalProperty $entry "last_failed_at" "")
                last_repair_failed_at = [string](Get-OptionalProperty $entry "last_repair_failed_at" "")
            }
        }
    }
    catch {
        Write-WatchdogLog ("Public failure state was invalid and will be rebuilt: " + $_.Exception.Message)
    }
    return $result
}

function Write-PublicFailureState([hashtable]$State) {
    $stations = [ordered]@{}
    foreach ($key in @($State.Keys | Sort-Object { [int]$_ })) {
        $stations[[string]$key] = $State[$key]
    }
    [ordered]@{
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
        stations = $stations
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $publicFailureStatePath -Encoding UTF8
}

function Update-PublicFailureState([int[]]$FailedIds) {
    $state = Read-PublicFailureState
    $failed = @($FailedIds | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    $known = @($mounts | ForEach-Object { [int]$_.StationId } | Sort-Object -Unique)
    $now = (Get-Date).ToUniversalTime().ToString("o")
    foreach ($stationId in $known) {
        $key = [string]$stationId
        if ($failed -contains $stationId) {
            $existing = $state[$key]
            $state[$key] = [ordered]@{
                count = if ($null -eq $existing) { 1 } else { [int]$existing.count + 1 }
                first_failed_at = if ($null -eq $existing) { $now } else { [string]$existing.first_failed_at }
                last_failed_at = $now
                last_repair_failed_at = if ($null -eq $existing) { "" } else { [string]$existing.last_repair_failed_at }
            }
        }
        else {
            $null = $state.Remove($key)
        }
    }
    Write-PublicFailureState $state
    return @(
        $failed | Where-Object {
            [int]$state[[string]$_].count -ge $PublicFailureEscalationRuns
        }
    )
}

function Clear-PublicFailureState([int[]]$StationIds) {
    $state = Read-PublicFailureState
    foreach ($stationId in @($StationIds)) {
        $null = $state.Remove([string][int]$stationId)
    }
    Write-PublicFailureState $state
}

function Test-PublicFailureRetryReady([int]$StationId) {
    $state = Read-PublicFailureState
    $entry = $state[[string]$StationId]
    if ($null -eq $entry -or -not [string]$entry.last_repair_failed_at) {
        return $true
    }
    try {
        $failedAt = [datetime]::Parse([string]$entry.last_repair_failed_at).ToUniversalTime()
        return ((Get-Date).ToUniversalTime() - $failedAt).TotalMinutes -ge 30
    }
    catch {
        return $true
    }
}

function Mark-PublicRepairFailed([int[]]$StationIds) {
    $state = Read-PublicFailureState
    $now = (Get-Date).ToUniversalTime().ToString("o")
    foreach ($stationId in @($StationIds | Sort-Object -Unique)) {
        $key = [string][int]$stationId
        if ($null -ne $state[$key]) {
            $state[$key].last_repair_failed_at = $now
        }
    }
    Write-PublicFailureState $state
}

function Invoke-StationOutputRecovery(
    [int[]]$StationIds,
    [int[]]$PrimaryOnlyStationIds = @()
) {
    $workerRoot = Join-Path $DataRoot "State\StationWorkers"
    $recovered = @()
    $failed = @()
    foreach ($stationId in @($StationIds | Sort-Object -Unique)) {
        try {
            $heartbeatPath = Join-Path $workerRoot (
                "station-{0}.heartbeat.json" -f $stationId
            )
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
            $generation = [int](Get-OptionalProperty $heartbeat "generation" 0)
            if ($generation -le 0) {
                throw "Station worker generation is unavailable."
            }
            $configPath = Join-Path $workerRoot (
                "station-{0}-g{1}.json" -f $stationId, $generation
            )
            $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
            $commandPath = [string](Get-OptionalProperty $config "command_path" "")
            $ackPath = [string](Get-OptionalProperty $config "ack_path" "")
            if (-not $commandPath -or -not $ackPath) {
                throw "Station worker command channel is unavailable."
            }
            $commandId = [guid]::NewGuid().ToString("N")
            $command = [ordered]@{
                command_id = $commandId
                station_id = [int]$stationId
                generation = $generation
                method = if ($PrimaryOnlyStationIds -contains [int]$stationId) {
                    "recover_station_primary_output"
                }
                else {
                    "recover_station"
                }
                args = @([int]$stationId)
                kwargs = if ($PrimaryOnlyStationIds -contains [int]$stationId) {
                    @{}
                }
                else {
                    @{ force = $true }
                }
                created_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            } | ConvertTo-Json -Depth 6 -Compress
            $temporaryPath = "{0}.{1}.tmp" -f $commandPath, $commandId
            [IO.File]::WriteAllText(
                $temporaryPath,
                $command,
                [Text.UTF8Encoding]::new($false)
            )
            Move-Item -LiteralPath $temporaryPath -Destination $commandPath -Force

            $deadline = (Get-Date).AddSeconds(30)
            $acknowledgement = $null
            do {
                Start-Sleep -Milliseconds 250
                if (Test-Path -LiteralPath $ackPath -PathType Leaf) {
                    $candidate = Get-Content -LiteralPath $ackPath -Raw | ConvertFrom-Json
                    if ([string](Get-OptionalProperty $candidate "command_id" "") -eq $commandId) {
                        $acknowledgement = $candidate
                        break
                    }
                }
            } while ((Get-Date) -lt $deadline)
            if ($null -eq $acknowledgement) {
                throw "Station worker output recovery acknowledgement timed out."
            }
            if (-not [bool](Get-OptionalProperty $acknowledgement "ok" $false)) {
                throw "Station worker rejected output recovery."
            }
            $result = Get-OptionalProperty $acknowledgement "result" $null
            if (
                $null -eq $result -or
                -not [bool](Get-OptionalProperty $result "running" $false) -or
                -not [bool](Get-OptionalProperty $result "program_running" $false) -or
                -not [bool](Get-OptionalProperty $result "output_feed_active" $false)
            ) {
                throw "Station worker recovery did not restore an active broadcast output."
            }
            $recovered += [pscustomobject]@{
                station_id = [int]$stationId
                generation = $generation
                mode = "output_branch_recovery"
            }
        }
        catch {
            $failed += [pscustomobject]@{
                station_id = [int]$stationId
                error = $_.Exception.Message
            }
        }
    }
    return [pscustomobject]@{
        recovered = @($recovered)
        failed = @($failed)
        failed_ids = @($failed | ForEach-Object { [int]$_.station_id })
    }
}

function Test-PrimaryOnlyRecoveryPreferred([int]$StationId) {
    try {
        $heartbeatPath = Join-Path $DataRoot (
            "State\StationWorkers\station-{0}.heartbeat.json" -f $StationId
        )
        $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
        $runtime = Get-OptionalProperty $heartbeat "runtime_status" $null
        if (
            $null -eq $runtime -or
            -not [bool](Get-OptionalProperty $runtime "program_running" $false) -or
            [bool](Get-OptionalProperty $runtime "program_pcm_stalled" $true) -or
            [double](Get-OptionalProperty $runtime "program_pcm_age_seconds" 999999.0) -gt $TransportFreshnessSeconds
        ) {
            return $false
        }

        $primary = Get-OptionalProperty $runtime "icecast_mount_health" $null
        $primaryStale = (
            [bool](Get-OptionalProperty $primary "writer_failed" $false) -or
            [bool](Get-OptionalProperty $primary "network_failed" $false) -or
            [double](Get-OptionalProperty $primary "last_write_age_seconds" 999999.0) -gt $TransportFreshnessSeconds -or
            [double](Get-OptionalProperty $primary "last_network_write_age_seconds" 999999.0) -gt $TransportFreshnessSeconds
        )
        if (-not $primaryStale) {
            return $false
        }

        $required = Get-OptionalProperty $runtime "required_outputs" $null
        $requiredExtraBranches = @(
            $required.PSObject.Properties |
                Where-Object { $_.Name.StartsWith("icecast:") -and [bool]$_.Value } |
                ForEach-Object { [string]$_.Name }
        )
        if ($requiredExtraBranches.Count -eq 0) {
            return $false
        }
        $extras = @(Get-OptionalProperty $runtime "extra_icecast_mounts" @())
        foreach ($branch in $requiredExtraBranches) {
            $extra = $extras | Where-Object { [string]$_.branch -eq $branch } | Select-Object -First 1
            if ($null -eq $extra) {
                return $false
            }
            $health = Get-OptionalProperty $extra "health" $null
            if (
                -not [bool](Get-OptionalProperty $health "writer_running" $false) -or
                -not [bool](Get-OptionalProperty $health "network_writer_running" $false) -or
                [bool](Get-OptionalProperty $health "writer_failed" $false) -or
                [bool](Get-OptionalProperty $health "network_failed" $false) -or
                [double](Get-OptionalProperty $health "last_write_age_seconds" 999999.0) -gt $TransportFreshnessSeconds -or
                [double](Get-OptionalProperty $health "last_network_write_age_seconds" 999999.0) -gt $TransportFreshnessSeconds
            ) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
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
    if (-not $script:WatchdogToken) {
        return
    }
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
    Invoke-PendingBackendSourceReload
    Start-BackendIfNeeded
    $script:WatchdogToken = Get-WatchdogToken
    Write-WatchdogLog "Starting watchdog cycle; local worker health does not depend on backend API availability."
    $firstSnapshot = Get-WatchdogStatusSnapshot
    $firstProfilesHealthy = if ($null -eq $firstSnapshot) { $null } else {
        [bool](Test-ManagedProfilesHealthy $firstSnapshot)
    }
    $stationIds = @(Get-WatchdogStationIds $firstSnapshot)
    if ($stationIds.Count -eq 0) {
        throw "Watchdog status did not include any station runtimes."
    }
    $firstFailed = @(
        $stationIds | Where-Object {
            -not [bool](Get-LocalTransportState ([int]$_)).healthy
        }
    )
    if ($firstFailed.Count -eq 0 -and $firstProfilesHealthy -ne $false) {
        Clear-PublicFailureState @($mounts | ForEach-Object { [int]$_.StationId } | Sort-Object -Unique)
        $profilesMessage = if ($null -eq $firstProfilesHealthy) {
            "Managed profiles were not checked because the API is unavailable."
        }
        else { "Managed profiles are healthy." }
        Send-Report "ok" "Local source writers are healthy. $profilesMessage No listener probes were run." @() ([bool]$firstProfilesHealthy)
        Write-WatchdogLog "OK: local source writers healthy; $profilesMessage"
        exit 0
    }

    Write-WatchdogLog (
        "First local check failed stations={0} managed_profiles_ok={1}; waiting 30 seconds for confirmation." -f
        ($firstFailed -join ","), $(if ($null -eq $firstProfilesHealthy) { "unverified" } else { $firstProfilesHealthy })
    )
    Start-Sleep -Seconds 30
    $secondSnapshot = Get-WatchdogStatusSnapshot
    $secondProfilesHealthy = if ($null -eq $secondSnapshot) { $null } else {
        [bool](Test-ManagedProfilesHealthy $secondSnapshot)
    }
    $stationIds = @(Get-WatchdogStationIds $secondSnapshot)
    if ($stationIds.Count -eq 0) {
        throw "Second watchdog status did not include any station runtimes."
    }
    $secondFailed = @(
        $stationIds | Where-Object {
            -not [bool](Get-LocalTransportState ([int]$_)).healthy
        }
    )
    $profileRepair = $secondProfilesHealthy -eq $false
    if ($secondFailed.Count -eq 0 -and -not $profileRepair) {
        Clear-PublicFailureState @($mounts | ForEach-Object { [int]$_.StationId } | Sort-Object -Unique)
        $message = "Local source health recovered on confirmation; no repair was performed."
        $profilesOk = [bool]$secondProfilesHealthy
        if ($null -eq $secondProfilesHealthy) {
            $message += " Managed profiles remain unverified because the API is unavailable."
        }
        Send-Report "transient" $message @() $profilesOk
        Write-WatchdogLog $message
        exit 0
    }
    $repairableFailed = @($secondFailed | Sort-Object -Unique)
    if (Test-RepairCooldown) {
        Send-Report "cooldown" "Confirmed local source failure; the 15-minute successful-repair cooldown prevented a loop." $repairableFailed (-not $profileRepair)
        Write-WatchdogLog "Confirmed local source failure suppressed by the successful-repair cooldown."
        exit 21
    }

    # Repair only source writers whose local heartbeat remained unhealthy in
    # both snapshots. Listener GET/decoder failures never restart a station.
    $primaryOnlyStationIds = @(
        $repairableFailed | Where-Object { Test-PrimaryOnlyRecoveryPreferred ([int]$_) }
    )
    $outputRecovery = Invoke-StationOutputRecovery `
        -StationIds $repairableFailed `
        -PrimaryOnlyStationIds $primaryOnlyStationIds
    $apiStationIds = @(
        (Get-OptionalProperty $secondSnapshot "stations" @()) |
            ForEach-Object { [int](Get-OptionalProperty $_ "station_id" 0) }
    )
    $fallbackStationIds = @(
        $outputRecovery.failed_ids | Where-Object {
            $apiStationIds -contains [int]$_
        }
    )
    $repair = $null
    if ($null -ne $secondSnapshot -and ($fallbackStationIds.Count -gt 0 -or $profileRepair)) {
        try {
            $repair = Invoke-WatchdogApi -Method POST -Path "/api/watchdog/repair" -Body @{
                station_ids = @($fallbackStationIds)
                force_station_ids = @($fallbackStationIds)
                repair_managed_profiles = $profileRepair
            }
            if (-not [bool]$repair.ok) {
                Write-WatchdogLog "Backend repair API returned an incomplete result; continuing local verification."
                $repair = $null
            }
        }
        catch {
            Write-WatchdogLog ("Backend repair API unavailable; continuing local verification: " + $_.Exception.Message)
        }
    }
    elseif ($null -eq $secondSnapshot) {
        Write-WatchdogLog "Backend repair deferred because the status API is unavailable. Local output recovery has still run."
    }
    $restartedCount = @($outputRecovery.recovered).Count + @((Get-OptionalProperty $repair "restarted" @())).Count
    $deferredCount = @((Get-OptionalProperty $repair "deferred" @())).Count
    if ($restartedCount -eq 0 -and $deferredCount -gt 0 -and -not $profileRepair) {
        Send-Report "transient" "Affected source recovered before repair; worker restart was suppressed." @() $true
        Write-WatchdogLog "Source transport recovered before repair; worker restart suppressed."
        exit 0
    }
    # Output recovery staggers reconnects to protect the origin. Verify against
    # local source-writer timestamps after that window plus encoder warm-up.
    Start-Sleep -Seconds 45
    $finalSnapshot = Get-WatchdogStatusSnapshot
    $finalProfilesHealthy = if ($null -eq $finalSnapshot) { $null } else {
        [bool](Test-ManagedProfilesHealthy $finalSnapshot)
    }
    $finalStationIds = @(Get-WatchdogStationIds $finalSnapshot)
    $finalFailed = @(
        $finalStationIds | Where-Object {
            -not [bool](Get-LocalTransportState ([int]$_)).healthy
        }
    )
    if ($finalFailed.Count -gt 0 -or $finalProfilesHealthy -eq $false) {
        Mark-PublicRepairFailed $finalFailed
        $message = "Repair completed but local source verification still failed for station ids=" + ($finalFailed -join ",")
        Send-Report "failed" $message $finalFailed ([bool]$finalProfilesHealthy)
        Write-WatchdogLog (
            "Repair final local verification failed stations=" + ($finalFailed -join ",")
        )
        exit 22
    }
    # Only successful repairs enter cooldown. A failed final verification stays
    # eligible for a later attempt, while healthy streams remain untouched.
    if ($restartedCount -gt 0) {
        Save-RepairState ("audio={0};profiles={1}" -f ($repairableFailed -join ","), $profileRepair)
    }
    Clear-PublicFailureState @($mounts | ForEach-Object { [int]$_.StationId } | Sort-Object -Unique)
    $verifiedMessage = if ($null -eq $finalProfilesHealthy) {
        "Local source repair and verification passed; managed profiles remain unverified because the API is unavailable."
    }
    else { "Confirmed local source failures were repaired and verified." }
    Send-Report "repaired" $verifiedMessage @() ([bool]$finalProfilesHealthy)
    Write-WatchdogLog $verifiedMessage
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
