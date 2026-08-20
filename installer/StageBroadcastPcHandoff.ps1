#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Stages a Broadcast-PC handoff into fixed, service-safe locations.

.DESCRIPTION
This helper validates, stages, installs clean dependencies, writes protected
remapped configuration, and atomically publishes source trees. It never starts,
stops, registers, or reconfigures an SCM service or scheduled task.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$HandoffRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$JukeMusicRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$VotingMusicRoot,
    [string]$NodePath = 'C:\Program Files\nodejs\node.exe',
    [string]$PythonPath = 'C:\Program Files\Python312\python.exe',
    [string]$FfmpegPath = 'C:\RadioTEDU\tools\ffmpeg.exe',
    [string]$FfprobePath = 'C:\RadioTEDU\tools\ffprobe.exe',
    [string]$PythonLockPath = (Join-Path $PSScriptRoot 'requirements\radiotedu-handoff-py312.lock.txt'),
    [switch]$ReplaceExistingTargets
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$radioRoot = 'C:\RadioTEDU'
$programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
$secretRoot = Join-Path $programData 'RadioTEDU\secrets'
$aiRoot = Join-Path $programData 'RadioTEDU\ai-broadcast-agent'
$aiConfigRoot = Join-Path $aiRoot 'config'
$stageRoot = Join-Path $radioRoot '.staging'
$rollbackRoot = Join-Path $radioRoot '.rollback'
$serviceToolsRoot = Join-Path $radioRoot 'tools'
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$stageVersionRoot = Join-Path $stageRoot $stamp

function Fail([string]$Message) { throw "Broadcast-PC handoff staging blocked: $Message" }
function Assert-Directory([string]$Path, [string]$Description) {
    if (-not [IO.Path]::IsPathFullyQualified($Path)) { Fail "$Description is not absolute." }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Fail "$Description is missing." }
}
function Assert-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "$Description is missing." }
}
function Assert-OutsideHandoff([string]$Path, [string]$Description) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullHandoff = [IO.Path]::GetFullPath($HandoffRoot)
    if ($fullPath.StartsWith($fullHandoff, [StringComparison]::OrdinalIgnoreCase) -or $fullPath -match '(?i)^[A-Z]:\\Users\\') {
        Fail "$Description must be a clean installed runtime or operator library, not a handoff or user-profile copy."
    }
}
function Assert-MediaRoot([string]$Path, [string]$Description) {
    Assert-Directory $Path $Description
    Assert-OutsideHandoff $Path $Description
    $audio = Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction Stop |
        Where-Object { $_.Extension -match '(?i)^\.(mp3|wav|flac|ogg|m4a|aac|opus|aiff|wma)$' } | Select-Object -First 1
    if ($null -eq $audio) { Fail "$Description has no supported audio files." }
}
function Assert-RestrictedAcl([string]$Path) {
    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { Fail 'A protected destination has inherited ACLs enabled.' }
    foreach ($rule in @($acl.Access)) {
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
        catch { Fail 'A protected destination has an unresolved ACL identity.' }
        if ($sid -notin $allowed) { Fail 'A protected destination has an unexpected ACL principal.' }
    }
}
function Protect-Path([string]$Path, [switch]$Tree) {
    $arguments = @($Path, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/c')
    if ($Tree) { $arguments += '/t' }
    & icacls.exe @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'Unable to apply protected ACLs.' }
    Assert-RestrictedAcl $Path
}
function Read-EnvironmentFile([string]$Path) {
    $values = [ordered]@{}
    foreach ($raw in [IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { Fail 'A handoff environment file has invalid syntax.' }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { Fail 'A handoff environment file has an invalid variable name.' }
        $values[$name] = $line.Substring($separator + 1).Trim()
    }
    return $values
}
function Write-EnvironmentFile([string]$Path, [System.Collections.IDictionary]$Values) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $lines = foreach ($name in $Values.Keys) { "$name=$($Values[$name])" }
        [IO.File]::WriteAllLines($temporary, [string[]]$lines, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
        Protect-Path $Path
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Resolve-OfficialRuntime([string]$Path, [string]$Name, [string]$Argument, [switch]$AllowUserProfile) {
    Assert-File $Path "$Name runtime"
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail "$Name runtime cannot be a reparse point." }
    if ($AllowUserProfile) {
        if ([IO.Path]::GetFullPath($Path).StartsWith([IO.Path]::GetFullPath($HandoffRoot), [StringComparison]::OrdinalIgnoreCase)) { Fail "$Name runtime cannot be copied from the handoff." }
    }
    else { Assert-OutsideHandoff $Path "$Name runtime" }
    & $Path $Argument 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "$Name runtime did not pass its version check." }
    return [IO.Path]::GetFullPath($Path)
}
function Assert-PythonLock([string]$Path) {
    Assert-File $Path 'Python lockfile'
    $entryCount = 0
    $hashCount = 0
    $activeEntry = $false
    foreach ($raw in [IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        if ($line -match '^[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+\s*(\\)?$') {
            if ($activeEntry -and $hashCount -eq 0) { Fail 'Python lockfile has an un-hashed dependency.' }
            $entryCount++
            $hashCount = 0
            $activeEntry = $true
            continue
        }
        if ($line -match '^--hash=sha256:[a-fA-F0-9]{64}\s*(\\)?$' -and $activeEntry) {
            $hashCount++
            continue
        }
        Fail 'Python lockfile is not fully pinned and hash-locked.'
    }
    if (-not $activeEntry -or $hashCount -eq 0) { Fail 'Python lockfile has no complete hash-locked dependency entries.' }
    return [IO.Path]::GetFullPath($Path)
}
function Invoke-CleanNodeInstall([string]$PackageDirectory) {
    Assert-File (Join-Path $PackageDirectory 'package-lock.json') 'Node lockfile'
    Assert-File (Join-Path $PackageDirectory 'package.json') 'Node package manifest'
    Push-Location $PackageDirectory
    try {
        & $script:resolvedNode 'ci' | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'A clean npm ci dependency installation failed.' }
    }
    finally { Pop-Location }
}
function Invoke-CleanPythonInstall([string]$SourceRoot, [string]$LockPath) {
    $venv = Join-Path $SourceRoot '.venv'
    & $script:resolvedPython '-m' 'venv' $venv | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'Python virtual-environment creation failed.' }
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    Assert-File $venvPython 'new Python virtual environment'
    & $venvPython '-m' 'pip' 'install' '--require-hashes' '-r' $LockPath | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'Hash-locked Python dependency installation failed.' }
}
function Assert-TargetMayPublish([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) { return }
    if (@(Get-ChildItem -LiteralPath $Target -Force -ErrorAction Stop).Count -gt 0 -and -not $ReplaceExistingTargets) {
        Fail "Existing nonempty target requires -ReplaceExistingTargets: $Target"
    }
}
function Assert-NoActiveAgentServices {
    foreach ($name in @('RadioTEDU.OnAir.JukeLocalMediaAgent', 'RadioTEDU.OnAir.VotingRadioAgent', 'RadioTEDU.OnAir.AiPublicStateAgent')) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($null -ne $service -and $service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            Fail 'Stop the reviewed Broadcast-PC agent services before staging replacement source trees.'
        }
    }
}
function Publish-AtomicDirectory([string]$Stage, [string]$Target, [string]$Name) {
    $backup = Join-Path $rollbackRoot "$Name-$stamp"
    if (Test-Path -LiteralPath $Target) {
        if (@(Get-ChildItem -LiteralPath $Target -Force).Count -gt 0) { Move-Item -LiteralPath $Target -Destination $backup -ErrorAction Stop }
        else { Remove-Item -LiteralPath $Target -Force -ErrorAction Stop }
    }
    Move-Item -LiteralPath $Stage -Destination $Target -ErrorAction Stop
    Protect-Path $Target -Tree
}
function Publish-ServiceTool([string]$StageFile, [string]$TargetFile, [string]$Name) {
    if (Test-Path -LiteralPath $TargetFile) {
        $existing = Get-Item -LiteralPath $TargetFile -Force
        if (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'A service tool target cannot be a reparse point.' }
        if (-not $ReplaceExistingTargets) { Fail "Existing service tool requires -ReplaceExistingTargets: $Name" }
        $toolRollback = Join-Path $rollbackRoot "tools-$stamp"
        New-Item -ItemType Directory -Path $toolRollback -Force | Out-Null
        Move-Item -LiteralPath $TargetFile -Destination (Join-Path $toolRollback $Name) -ErrorAction Stop
    }
    Move-Item -LiteralPath $StageFile -Destination $TargetFile -ErrorAction Stop
    Protect-Path $TargetFile
}

$HandoffRoot = [IO.Path]::GetFullPath($HandoffRoot)
$JukeMusicRoot = [IO.Path]::GetFullPath($JukeMusicRoot)
$VotingMusicRoot = [IO.Path]::GetFullPath($VotingMusicRoot)
Assert-Directory $HandoffRoot 'Handoff root'
$manifestGenerator = Join-Path $PSScriptRoot 'NewBroadcastPcHandoffManifest.ps1'
Assert-File $manifestGenerator 'Handoff manifest validator'
& $manifestGenerator -HandoffRoot $HandoffRoot -ManifestPath (Join-Path $HandoffRoot 'handoff-manifest.json') -ValidateOnly | Out-Null
foreach ($directory in @('source\juke-local', 'source\voting\rtjukebox', 'source\RadioTEDU', 'config', 'secrets')) { Assert-Directory (Join-Path $HandoffRoot $directory) 'Required handoff snapshot directory' }
foreach ($file in @(
    'source\juke-local\media-agent\package.json', 'source\juke-local\media-agent\package-lock.json', 'source\juke-local\media-agent\server.js',
    'source\voting\rtjukebox\tools\local-voting-agent\package.json', 'source\voting\rtjukebox\tools\local-voting-agent\package-lock.json', 'source\voting\rtjukebox\tools\local-voting-agent\scripts\voting-supervisor.mjs',
    'source\RadioTEDU\requirements.txt', 'config\radiotedu-app.env', 'config\voting-service.json',
    'secrets\ai-broadcast-agent.env', 'secrets\juke-media-agent.env', 'secrets\voting-agent.env', 'secrets\web-hmac.env')) { Assert-File (Join-Path $HandoffRoot $file) 'Required handoff file' }
Assert-MediaRoot $JukeMusicRoot 'Juke operator music root'
Assert-MediaRoot $VotingMusicRoot 'Voting operator music root'
$script:resolvedNode = Resolve-OfficialRuntime $NodePath 'Node.js' '--version'
$script:resolvedPython = Resolve-OfficialRuntime $PythonPath 'Python' '--version'
$resolvedFfmpeg = Resolve-OfficialRuntime $FfmpegPath 'FFmpeg' '-version' -AllowUserProfile
$resolvedFfprobe = Resolve-OfficialRuntime $FfprobePath 'FFprobe' '-version' -AllowUserProfile
$serviceFfmpegPath = Join-Path $serviceToolsRoot 'ffmpeg.exe'
$serviceFfprobePath = Join-Path $serviceToolsRoot 'ffprobe.exe'
if (Test-Path -LiteralPath $serviceToolsRoot) { Assert-RestrictedAcl $serviceToolsRoot }
$PythonLockPath = Assert-PythonLock ([IO.Path]::GetFullPath($PythonLockPath))
foreach ($target in @((Join-Path $radioRoot 'juke-local'), (Join-Path $radioRoot 'voting'), (Join-Path $radioRoot 'RadioTEDU'))) { Assert-TargetMayPublish $target }
Assert-NoActiveAgentServices

if (-not $PSCmdlet.ShouldProcess($stageVersionRoot, 'stage validated handoff sources and clean dependencies')) { return }
New-Item -ItemType Directory -Path $stageVersionRoot -Force | Out-Null
New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stageVersionRoot 'tools') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $HandoffRoot 'source\juke-local') -Destination (Join-Path $stageVersionRoot 'juke-local') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $HandoffRoot 'source\voting') -Destination (Join-Path $stageVersionRoot 'voting') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $HandoffRoot 'source\RadioTEDU') -Destination (Join-Path $stageVersionRoot 'RadioTEDU') -Recurse -Force
Copy-Item -LiteralPath $resolvedFfmpeg -Destination (Join-Path $stageVersionRoot 'tools\ffmpeg.exe') -Force
Copy-Item -LiteralPath $resolvedFfprobe -Destination (Join-Path $stageVersionRoot 'tools\ffprobe.exe') -Force
Copy-Item -LiteralPath $PythonLockPath -Destination (Join-Path $stageVersionRoot 'RadioTEDU\requirements.lock') -Force
Invoke-CleanNodeInstall (Join-Path $stageVersionRoot 'juke-local\media-agent')
Invoke-CleanNodeInstall (Join-Path $stageVersionRoot 'voting\rtjukebox\tools\local-voting-agent')
Invoke-CleanPythonInstall (Join-Path $stageVersionRoot 'RadioTEDU') (Join-Path $stageVersionRoot 'RadioTEDU\requirements.lock')

foreach ($directory in @($secretRoot, $aiConfigRoot, (Join-Path $aiRoot 'state'), (Join-Path $aiRoot 'logs'))) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    Protect-Path $directory -Tree
}
if (-not (Test-Path -LiteralPath $serviceToolsRoot)) {
    New-Item -ItemType Directory -Path $serviceToolsRoot -Force | Out-Null
    Protect-Path $serviceToolsRoot
}
Publish-ServiceTool (Join-Path $stageVersionRoot 'tools\ffmpeg.exe') $serviceFfmpegPath 'ffmpeg.exe'
Publish-ServiceTool (Join-Path $stageVersionRoot 'tools\ffprobe.exe') $serviceFfprobePath 'ffprobe.exe'
$jukeEnv = Read-EnvironmentFile (Join-Path $HandoffRoot 'secrets\juke-media-agent.env')
$jukeEnv['LOCAL_MUSIC_ROOT'] = $JukeMusicRoot; $jukeEnv['MEDIA_AGENT_BIND_HOST'] = '127.0.0.1'; $jukeEnv['MEDIA_AGENT_PORT'] = '3210'; $jukeEnv['AI_MIRROR_ENABLED'] = 'false'; $jukeEnv['AI_AUTOPLAY_ENABLED'] = 'false'; $jukeEnv['AI_MIRROR_FFMPEG_PATH'] = $serviceFfmpegPath
Write-EnvironmentFile (Join-Path $secretRoot 'juke-media-agent.env') $jukeEnv
$votingEnv = Read-EnvironmentFile (Join-Path $HandoffRoot 'secrets\voting-agent.env')
$votingEnv['MUSIC_LIBRARY_DIR'] = $VotingMusicRoot; $votingEnv['FFMPEG_PATH'] = $serviceFfmpegPath; $votingEnv['FFPROBE_PATH'] = $serviceFfprobePath; $votingEnv['PORT'] = '4317'; $votingEnv['HOST'] = '127.0.0.1'; $votingEnv['BIND_HOST'] = '127.0.0.1'; $votingEnv['LOCAL_HTTP_STREAM_PORT'] = '4320'; $votingEnv['LOCAL_HTTP_STREAM_BIND_HOST'] = '127.0.0.1'
Write-EnvironmentFile (Join-Path $secretRoot 'voting-agent.env') $votingEnv
$aiEnv = Read-EnvironmentFile (Join-Path $HandoffRoot 'secrets\ai-broadcast-agent.env')
$aiEnv['STATE_FILE'] = (Join-Path $aiRoot 'state\state.json'); $aiEnv['LOG_FILE'] = (Join-Path $aiRoot 'logs\agent.log'); $aiEnv['EN_STATUS_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\runtime\temporary-dual-station\radiotedu-en\status.json'; $aiEnv['FR_STATUS_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\runtime\temporary-dual-station\radiotedu-fr\status.json'; $aiEnv['EN_HISTORY_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\runtime\temporary-dual-station\radiotedu-en\history.jsonl'; $aiEnv['FR_HISTORY_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\runtime\temporary-dual-station\radiotedu-fr\history.jsonl'; $aiEnv['EN_DATABASE_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\stations\radiotedu-en\radio.db'; $aiEnv['FR_DATABASE_FILE'] = 'C:\RadioTEDU\RadioTEDU\data\stations\radiotedu-fr\radio.db'
Write-EnvironmentFile (Join-Path $aiConfigRoot 'agent.env') $aiEnv
Copy-Item -LiteralPath (Join-Path $HandoffRoot 'secrets\web-hmac.env') -Destination (Join-Path $secretRoot 'web-hmac.env') -Force; Protect-Path (Join-Path $secretRoot 'web-hmac.env')
Copy-Item -LiteralPath (Join-Path $HandoffRoot 'config\radiotedu-app.env') -Destination (Join-Path $aiConfigRoot 'radiotedu-app.env') -Force; Protect-Path (Join-Path $aiConfigRoot 'radiotedu-app.env')
foreach ($publication in @(
    @{ Stage = (Join-Path $stageVersionRoot 'juke-local'); Target = (Join-Path $radioRoot 'juke-local'); Name = 'juke-local' },
    @{ Stage = (Join-Path $stageVersionRoot 'voting'); Target = (Join-Path $radioRoot 'voting'); Name = 'voting' },
    @{ Stage = (Join-Path $stageVersionRoot 'RadioTEDU'); Target = (Join-Path $radioRoot 'RadioTEDU'); Name = 'RadioTEDU' })) { Publish-AtomicDirectory $publication.Stage $publication.Target $publication.Name }
Write-Verbose 'Broadcast-PC handoff staged and published. Services and tasks were intentionally not started or modified.'
