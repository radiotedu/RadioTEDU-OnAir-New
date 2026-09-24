param([Parameter(Mandatory=$true)][string]$ScriptPath)
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Watchdog syntax errors' }
$definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Remove-PublicAudioProbeListener'}, $true)
if (-not $definition) { throw 'Cleanup function missing' }
Invoke-Expression $definition.Extent.Text
$script:removed = @()
function Invoke-RestMethod {
    param($Method, $Uri, $TimeoutSec)
    # Invoke-RestMethod emits a JSON array as one pipeline object.
    Write-Output -NoEnumerate @(
        [pscustomobject]@{Id=42;UserAgent='RadioTEDU-AudioWatch/test'},
        [pscustomobject]@{Id=43;UserAgent='ordinary-listener'}
    )
}
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, $Method, $Uri, $TimeoutSec)
    $script:removed += [string]$Uri
}
Remove-PublicAudioProbeListener ([pscustomobject]@{
    Mount=[pscustomobject]@{Url='http://origin.invalid:11154/lofi'}
    UserAgent='RadioTEDU-AudioWatch/test'
})
if ($script:removed.Count -ne 1 -or $script:removed[0] -ne 'http://origin.invalid:11154/admin/killclient?id=42') {
    throw "Expected removal of only the matching probe; removed count: $($script:removed.Count)"
}
'PASS: removes the matching probe from a JSON array and preserves ordinary listeners'

foreach ($name in @('Get-OptionalProperty', 'Get-LocalTransportState')) {
    $definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    Invoke-Expression $definition.Extent.Text
}
$DataRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('onair-watchdog-test-' + [guid]::NewGuid().ToString('N'))
$stateDir = Join-Path (Join-Path $DataRoot 'State') 'StationWorkers'
$null = New-Item -ItemType Directory -Path $stateDir -Force
$heartbeatPath = Join-Path $stateDir 'station-2.heartbeat.json'
$TransportFreshnessSeconds = 10
$heartbeat = @{
    updated_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    running = $true; transport_healthy = $false
    runtime_status = @{
        running = $true; program_running = $true; program_pcm_stalled = $false
        program_pcm_age_seconds = 0.01; icecast_sink_running = $true
        icecast_mount_health = @{
            process_running = $true; writer_running = $true
            writer_failed = $false; network_failed = $false
            last_write_age_seconds = 0.01; last_network_write_age_seconds = 0.01
            writer_backpressured = $true; writer_backpressure_age_seconds = 300
            queued_pcm_seconds = 4; pcm_queue_capacity_chunks = 1024
        }
    }
}
try {
    $heartbeat | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $heartbeatPath
    if (-not (Get-LocalTransportState 2).healthy) { throw 'Fresh source writes incorrectly marked unhealthy' }
    $heartbeat.runtime_status.icecast_mount_health.last_network_write_age_seconds = 30
    $heartbeat | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $heartbeatPath
    if ((Get-LocalTransportState 2).healthy) { throw 'Stalled network writer incorrectly marked healthy' }
}
finally {
    Remove-Item -LiteralPath $heartbeatPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stateDir -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $DataRoot 'State') -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $DataRoot -ErrorAction SilentlyContinue
}
'PASS: fresh writes override stale aggregate flags; actual writer stalls remain repairable'
$content = [System.IO.File]::ReadAllText($ScriptPath)
$start = $content.IndexOf('    $locallyUnhealthyFailed = @(Get-RepairableStationIds $secondFailed)')
$end = $content.IndexOf('    $primaryOnlyRepairIds = @()', $start)
if ($start -lt 0 -or $end -lt 0) { throw 'Recovery decision block missing' }
$decision = $content.Substring($start, $end - $start)
$secondFailed = @(2)
$escalatedPublicFailures = @(2)
function Test-PublicFailureRetryReady { param($Id) return $true }
function Get-RepairableStationIds { param($Ids) return @() }
Invoke-Expression $decision
if ($repairableFailed.Count -ne 0) { throw 'Public-only failure forced a healthy source restart' }
function Get-RepairableStationIds { param($Ids) return @(2) }
Invoke-Expression $decision
if ($repairableFailed.Count -ne 1 -or $repairableFailed[0] -ne 2) { throw 'Actual local failure lost its repair path' }
'PASS: public-only failures preserve sources; actual local failures still trigger repair'
