#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Fail-closed provisioning for the separately deployed Broadcast-PC agents.

.DESCRIPTION
This helper never copies credentials, prints environment values, or enables a
service until all non-secret foreground verification evidence is current. It
creates one ServiceHost configuration and one delayed-auto SCM service for each
agent only after the operator has placed the deployment at the fixed paths.

Run with -WhatIf first. Existing SCM services are never changed unless
-ReplaceExistingServices is explicitly supplied.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$EvidencePath = 'C:\ProgramData\RadioTEDU\OnAir\Commissioning\preflight-evidence.json',
    [ValidateRange(1, 168)]
    [int]$MaximumEvidenceAgeHours = 24,
    [switch]$ReplaceExistingServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$onAirRoot = Join-Path $programData 'RadioTEDU\OnAir'
$serviceRoot = Join-Path $onAirRoot 'Services'
$hostPath = Join-Path $env:ProgramFiles 'RadioTEDU\OnAir\service-host\RadioTEDU-OnAir-ServiceHost.exe'
$nodePath = Join-Path $env:ProgramFiles 'nodejs\node.exe'
$aiPythonPath = 'C:\RadioTEDU\RadioTEDU\.venv\Scripts\python.exe'
$installedRoot = Split-Path -Parent $PSScriptRoot
$publicStateAgentPath = Join-Path $installedRoot 'tools\radiotedu_public_state_agent.py'
$jukeRoot = 'C:\RadioTEDU\juke-local\media-agent'
$votingRoot = 'C:\RadioTEDU\voting\rtjukebox\tools\local-voting-agent'
$radioTeduRoot = 'C:\RadioTEDU\RadioTEDU'
$handoffRoot = 'C:\RadioTEDU-Handoff'
$secretsRoot = 'C:\ProgramData\RadioTEDU\secrets'
$aiConfigRoot = 'C:\ProgramData\RadioTEDU\ai-broadcast-agent\config'

function Fail([string]$Message) {
    throw "Broadcast-PC provisioning blocked: $Message"
}

function Assert-ExactDirectory([string]$Path) {
    if (-not [IO.Path]::IsPathFullyQualified($Path) -or -not $Path.StartsWith('C:\', [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'A required path is not an absolute C: path.'
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Fail "Required directory is missing: $Path"
    }
}

function Assert-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "Required file is missing: $Path"
    }
}

function Test-RestrictedAcl([string]$Path) {
    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        Fail "ACL inheritance is enabled on protected path: $Path"
    }
    foreach ($rule in @($acl.Access)) {
        try {
            $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        }
        catch {
            Fail "ACL identity cannot be resolved on protected path: $Path"
        }
        if ($sid -notin $allowed) {
            Fail "Protected path has an unexpected ACL principal: $Path"
        }
    }
}

function Set-RestrictedAcl([string]$Path) {
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $administratorsSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administratorsSid)
    foreach ($sid in @($systemSid, $administratorsSid)) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::None,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    Test-RestrictedAcl $Path
}

function Read-EnvironmentFile([string]$Path) {
    $values = @{}
    foreach ($raw in [IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { Fail 'A protected environment file has invalid syntax.' }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim().Trim('"')
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { Fail 'A protected environment file has an invalid variable name.' }
        $values[$name] = $value
    }
    return $values
}

function Assert-EnvironmentValue([hashtable]$Values, [string]$Name, [string]$Expected) {
    if (-not $Values.ContainsKey($Name) -or -not [string]::Equals([string]$Values[$Name], $Expected, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "A required non-secret safety setting is invalid: $Name"
    }
}

function Assert-TrueEvidence([object]$Checks, [string]$Name) {
    $property = $Checks.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -ne $true) {
        Fail "Current verification evidence does not attest to: $Name"
    }
}

function Assert-SafeEvidence([string]$Path) {
    Assert-File $Path
    $raw = [IO.File]::ReadAllText($Path)
    if ($raw -match '(?i)"[^"\r\n]*(password|secret|token|credential|api[_-]?key)[^"\r\n]*"\s*:') {
        Fail 'Verification evidence contains a secret-like field name.'
    }
    try { $evidence = $raw | ConvertFrom-Json -ErrorAction Stop } catch { Fail 'Verification evidence is not valid JSON.' }
    if ($evidence.schemaVersion -ne 1) { Fail 'Verification evidence has an unsupported schemaVersion.' }
    $generatedAt = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$evidence.generatedAtUtc, [ref]$generatedAt)) { Fail 'Verification evidence has no valid generatedAtUtc.' }
    if (([DateTimeOffset]::UtcNow - $generatedAt.ToUniversalTime()).TotalHours -gt $MaximumEvidenceAgeHours) { Fail 'Verification evidence is too old.' }
    if ($null -eq $evidence.checks) { Fail 'Verification evidence has no checks object.' }
    foreach ($name in @(
        'operatorMusicLibraryPresent',
        'jukeForegroundPassed', 'jukeLoopback3210', 'jukeWssConnected', 'jukeHeartbeat2xx', 'jukeReconnectPassed',
        'votingForegroundPassed', 'votingLoopback4317', 'votingLoopback4320', 'votingWssAuthenticated', 'votingReconnectPassed', 'votingIcecastConnected',
        'publicAiDecode30Seconds', 'publicEventEndpointChecked', 'radioTeduEnEndpoint200', 'radioTeduFrEndpoint200',
        'votingSoleAiSource', 'aiPublicStateMountless', 'aiPublicStateSourceFingerprintVerified')) {
        Assert-TrueEvidence $evidence.checks $name
    }
}

function Write-ProtectedServiceFile([string]$Path, [string[]]$Lines) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllLines($temporary, $Lines, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
        Set-RestrictedAcl $Path
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Write-ProtectedJsonFile([string]$Path, [hashtable]$Value) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
        Set-RestrictedAcl $Path
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Invoke-Sc([string[]]$Arguments, [string]$Operation) {
    & sc.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "SCM operation failed: $Operation" }
}

function Install-ServiceHostService([hashtable]$Definition) {
    $name = [string]$Definition.Name
    $config = [string]$Definition.Config
    $description = [string]$Definition.Description
    $imagePath = ('"{0}" --service-name "{1}" --config "{2}"' -f $hostPath, $name, $config)
    $existing = Get-CimInstance -ClassName Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue
    if ($null -ne $existing -and -not $ReplaceExistingServices) {
        Fail "An SCM service already exists; rerun only after review with -ReplaceExistingServices: $name"
    }
    if ($null -ne $existing) {
        if (-not $PSCmdlet.ShouldProcess($name, 'reconfigure existing ServiceHost SCM service')) { return }
        if ($existing.State -ne 'Stopped') { Stop-Service -Name $name -Force -ErrorAction Stop }
        Invoke-Sc @('config', $name, "binPath= $imagePath", 'start= delayed-auto', 'obj= LocalSystem', 'type= own') 'configure service'
    }
    else {
        if (-not $PSCmdlet.ShouldProcess($name, 'register delayed-auto ServiceHost SCM service')) { return }
        Invoke-Sc @('create', $name, "binPath= $imagePath", 'start= delayed-auto', 'obj= LocalSystem', 'type= own') 'create service'
    }
    Invoke-Sc @('failure', $name, 'reset= 86400', 'actions= restart/5000/restart/15000/restart/60000') 'configure bounded recovery'
    Invoke-Sc @('failureflag', $name, '1') 'enable failure recovery'
    Invoke-Sc @('description', $name, $description) 'set service description'
}

# Validate every required source, configuration and protected target before any write.
foreach ($directory in @(
    $handoffRoot,
    'C:\RadioTEDU\juke-local',
    'C:\RadioTEDU\voting',
    $radioTeduRoot,
    'C:\RadioTEDU\tools',
    $jukeRoot,
    $votingRoot,
    $secretsRoot,
    $aiConfigRoot,
    (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\state'),
    (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\logs'),
    $serviceRoot)) {
    Assert-ExactDirectory $directory
}
foreach ($file in @(
    (Join-Path $handoffRoot 'config\radiotedu-app.env'),
    (Join-Path $handoffRoot 'config\voting-service.json'),
    (Join-Path $handoffRoot 'secrets\ai-broadcast-agent.env'),
    (Join-Path $handoffRoot 'secrets\juke-media-agent.env'),
    (Join-Path $handoffRoot 'secrets\voting-agent.env'),
    (Join-Path $handoffRoot 'secrets\web-hmac.env'),
    $hostPath,
    $nodePath,
    $aiPythonPath,
    (Join-Path $jukeRoot 'server.js'),
    (Join-Path $votingRoot 'scripts\voting-supervisor.mjs'),
    $publicStateAgentPath,
    (Join-Path $secretsRoot 'juke-media-agent.env'),
    (Join-Path $secretsRoot 'voting-agent.env'),
    (Join-Path $secretsRoot 'web-hmac.env'),
    (Join-Path $aiConfigRoot 'radiotedu-app.env'))) {
    Assert-File $file
}
foreach ($protectedPath in @(
    $serviceRoot,
    $secretsRoot,
    $aiConfigRoot,
    (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\state'),
    (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\logs'),
    (Join-Path $secretsRoot 'juke-media-agent.env'),
    (Join-Path $secretsRoot 'voting-agent.env'),
    (Join-Path $secretsRoot 'web-hmac.env'),
    (Join-Path $aiConfigRoot 'radiotedu-app.env'))) {
    Test-RestrictedAcl $protectedPath
}

$jukeEnvironment = Read-EnvironmentFile (Join-Path $secretsRoot 'juke-media-agent.env')
Assert-EnvironmentValue $jukeEnvironment 'MEDIA_AGENT_BIND_HOST' '127.0.0.1'
Assert-EnvironmentValue $jukeEnvironment 'MEDIA_AGENT_PORT' '3210'
Assert-EnvironmentValue $jukeEnvironment 'AI_MIRROR_ENABLED' 'false'
Assert-EnvironmentValue $jukeEnvironment 'AI_AUTOPLAY_ENABLED' 'false'
$votingEnvironment = Read-EnvironmentFile (Join-Path $secretsRoot 'voting-agent.env')
Assert-EnvironmentValue $votingEnvironment 'PORT' '4317'
Assert-EnvironmentValue $votingEnvironment 'LOCAL_HTTP_STREAM_PORT' '4320'
Assert-SafeEvidence $EvidencePath

$publicStateConfigPath = Join-Path $aiConfigRoot 'public-state-agent.json'
if ($PSCmdlet.ShouldProcess($publicStateConfigPath, 'write protected mountless public-state agent configuration')) {
    Write-ProtectedJsonFile $publicStateConfigPath @{
        backend_root = $radioTeduRoot
        backend_env_file = (Join-Path $aiConfigRoot 'radiotedu-app.env')
        state_file = (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\state\public-state.json')
        log_file = (Join-Path $programData 'RadioTEDU\ai-broadcast-agent\logs\public-state-agent.log')
        poll_seconds = 5
    }
}

$definitions = @(
    @{ Name = 'RadioTEDU.OnAir.JukeLocalMediaAgent'; Config = (Join-Path $serviceRoot 'RadioTEDU.OnAir.JukeLocalMediaAgent.services'); Description = 'RadioTEDU Juke Local loopback media agent.'; Lines = @(
        '# Generated by ProvisionBroadcastPcAgents.ps1. Do not place credentials in this file.',
        "juke-local-media-agent|$nodePath|--env-file=`"$(Join-Path $secretsRoot 'juke-media-agent.env')`" server.js|$jukeRoot|true") },
    @{ Name = 'RadioTEDU.OnAir.VotingRadioAgent'; Config = (Join-Path $serviceRoot 'RadioTEDU.OnAir.VotingRadioAgent.services'); Description = 'RadioTEDU Voting agent; sole permitted Icecast /ai producer.'; Lines = @(
        '# Generated by ProvisionBroadcastPcAgents.ps1. Do not place credentials in this file.',
        "voting-radio-agent|$nodePath|--env-file=`"$(Join-Path $secretsRoot 'voting-agent.env')`" scripts\\voting-supervisor.mjs|$votingRoot|true") },
    @{ Name = 'RadioTEDU.OnAir.AiPublicStateAgent'; Config = (Join-Path $serviceRoot 'RadioTEDU.OnAir.AiPublicStateAgent.services'); Description = 'RadioTEDU AI and public-state agent; it must not source Icecast /ai.'; Lines = @(
        '# Generated by ProvisionBroadcastPcAgents.ps1. Do not place credentials in this file.',
        "ai-public-state-agent|$aiPythonPath|-u `"$publicStateAgentPath`" --config `"$publicStateConfigPath`"|$radioTeduRoot|true") }
)

foreach ($definition in $definitions) {
    if ($PSCmdlet.ShouldProcess($definition.Config, 'write protected ServiceHost configuration')) {
        Write-ProtectedServiceFile $definition.Config $definition.Lines
    }
}
foreach ($definition in $definitions) {
    Install-ServiceHostService $definition
}

Write-Verbose 'Broadcast-PC agent provisioning completed. No environment values or child arguments were logged.'
