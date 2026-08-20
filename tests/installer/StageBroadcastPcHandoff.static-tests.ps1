$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '..\..\installer\StageBroadcastPcHandoff.ps1'
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw "Staging helper has parse errors: $($errors.Count)" }
$source = [IO.File]::ReadAllText($scriptPath)
foreach ($required in @(
    'SupportsShouldProcess = $true',
    '#Requires -RunAsAdministrator',
    'ReplaceExistingTargets',
    'PythonLockPath',
    'NewBroadcastPcHandoffManifest.ps1',
    'handoff-manifest.json',
    'Publish-ServiceTool',
    'serviceFfmpegPath',
    'serviceFfprobePath',
    '-AllowUserProfile',
    'Assert-RestrictedAcl $serviceToolsRoot',
    'Assert-PythonLock',
    'radiotedu-handoff-py312.lock.txt',
    'RadioTEDU\requirements.lock',
    "'ci'",
    '--require-hashes',
    'AI_MIRROR_ENABLED',
    'AI_AUTOPLAY_ENABLED',
    'LOCAL_HTTP_STREAM_BIND_HOST',
    '.staging',
    '.rollback',
    'Move-Item -LiteralPath $Stage -Destination $Target',
    'Assert-NoActiveAgentServices',
    'Services and tasks were intentionally not started or modified'
)) {
    if (-not $source.Contains($required)) { throw "Staging helper is missing required fail-closed control: $required" }
}
foreach ($prohibited in @('Start-Service', 'Stop-Service', 'New-Service', 'sc.exe create', 'Register-ScheduledTask')) {
    if ($source.Contains($prohibited)) { throw "Staging helper must not alter service/task state: $prohibited" }
}
$lockPath = Join-Path $PSScriptRoot '..\..\installer\requirements\radiotedu-handoff-py312.lock.txt'
$entryCount = 0
$hashCount = 0
$activeEntry = $false
foreach ($raw in [IO.File]::ReadAllLines($lockPath)) {
    $line = $raw.Trim()
    if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
    if ($line -match '^[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+\s*(\\)?$') {
        if ($activeEntry -and $hashCount -eq 0) { throw 'Generated Python lock contains an un-hashed dependency.' }
        $entryCount++
        $hashCount = 0
        $activeEntry = $true
        continue
    }
    if ($line -match '^--hash=sha256:[a-fA-F0-9]{64}\s*(\\)?$' -and $activeEntry) {
        $hashCount++
        continue
    }
    throw 'Generated Python lock contains a non-pinned or malformed line.'
}
if (-not $activeEntry -or $entryCount -eq 0 -or $hashCount -eq 0) { throw 'Generated Python lock has no complete pinned and hashed entries.' }
$installerPath = Join-Path $PSScriptRoot '..\..\installer\RadioTEDUBroadcastRoomSetup.iss'
$installerSource = [IO.File]::ReadAllText($installerPath)
$lockLine = $installerSource.IndexOf('Source: "requirements\radiotedu-handoff-py312.lock.txt"')
if ($lockLine -lt $installerSource.IndexOf('[Files]') -or $lockLine -gt $installerSource.IndexOf('[Run]')) {
    throw 'The generated Python lock must be packaged as a file only.'
}
$mediaMapLine = $installerSource.IndexOf('Source: "templates\unified-media-source-map.json"')
if ($mediaMapLine -lt $installerSource.IndexOf('[Files]') -or $mediaMapLine -gt $installerSource.IndexOf('[Run]')) {
    throw 'The unified media source-map template must be packaged as a file only.'
}
$mediaMapPath = Join-Path $PSScriptRoot '..\..\installer\templates\unified-media-source-map.json'
$mediaMap = [IO.File]::ReadAllText($mediaMapPath) | ConvertFrom-Json
if ($mediaMap.mediaRoot -ne 'E:\RadioTEDU Media' -or $mediaMap.jukeMusicRoot -ne 'E:\RadioTEDU Media\Juke\Non-Turkish' -or $mediaMap.votingMusicRoot -ne 'E:\RadioTEDU Media\Voting') {
    throw 'The unified media source-map template paths are not canonical.'
}
$manifestGeneratorPath = Join-Path $PSScriptRoot '..\..\installer\NewBroadcastPcHandoffManifest.ps1'
$manifestGeneratorSource = [IO.File]::ReadAllText($manifestGeneratorPath)
foreach ($requiredManifestControl in @('schemaVersion', 'SHA-256', 'Get-FileHash', 'Duplicate or case-colliding', 'node_modules', '.venv', 'ValidateOnly')) {
    if (-not $manifestGeneratorSource.Contains($requiredManifestControl)) { throw "Handoff manifest generator is missing: $requiredManifestControl" }
}
$manifestLine = $installerSource.IndexOf('Source: "NewBroadcastPcHandoffManifest.ps1"')
if ($manifestLine -lt $installerSource.IndexOf('[Files]') -or $manifestLine -gt $installerSource.IndexOf('[Run]')) {
    throw 'The handoff manifest generator must be packaged as a file only.'
}
Write-Output 'StageBroadcastPcHandoff static tests passed.'
