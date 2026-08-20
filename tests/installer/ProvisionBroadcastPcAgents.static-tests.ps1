$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '..\..\installer\ProvisionBroadcastPcAgents.ps1'
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw "Provisioning helper has parse errors: $($errors.Count)" }
$source = [IO.File]::ReadAllText($scriptPath)
foreach ($required in @(
    'SupportsShouldProcess = $true',
    'AI_MIRROR_ENABLED',
    'AI_AUTOPLAY_ENABLED',
    'MEDIA_AGENT_BIND_HOST',
    'votingSoleAiSource',
    'publicAiDecode30Seconds',
    'publicEventEndpointChecked',
    'radioTeduEnEndpoint200',
    'radioTeduFrEndpoint200',
    'aiPublicStateMountless',
    'aiPublicStateSourceFingerprintVerified',
    'radiotedu_public_state_agent.py',
    'public-state-agent.json',
    'preflight-evidence.json',
    'start= delayed-auto',
    'restart/5000/restart/15000/restart/60000',
    'RadioTEDU.OnAir.JukeLocalMediaAgent',
    'RadioTEDU.OnAir.VotingRadioAgent',
    'RadioTEDU.OnAir.AiPublicStateAgent'
)) {
    if (-not $source.Contains($required)) { throw "Provisioning helper is missing required fail-closed control: $required" }
}
$installerPath = Join-Path $PSScriptRoot '..\..\installer\RadioTEDUBroadcastRoomSetup.iss'
$installerSource = [IO.File]::ReadAllText($installerPath)
$provisionLine = $installerSource.IndexOf('Source: "ProvisionBroadcastPcAgents.ps1"')
$filesSection = $installerSource.IndexOf('[Files]')
$runSection = $installerSource.IndexOf('[Run]')
if ($provisionLine -lt $filesSection -or $provisionLine -gt $runSection) {
    throw 'The provisioning helper must be packaged as a file only, never run by the installer.'
}
$agentLine = $installerSource.IndexOf('Source: "..\tools\radiotedu_public_state_agent.py"')
if ($agentLine -lt $filesSection -or $agentLine -gt $runSection) {
    throw 'The mountless public-state agent must be packaged as a file only.'
}
Write-Output 'ProvisionBroadcastPcAgents static tests passed.'
