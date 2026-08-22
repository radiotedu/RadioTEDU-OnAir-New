[CmdletBinding()]
param(
    [string]$DatabasePath = "C:\ProgramData\RadioTEDU\OnAir\cleanroom.db",
    [string]$HistoryRoot = "",
    [string]$GitHubRepository = "radiotedu/RadioTEDU-OnAir-Play-History",
    [string]$MirrorRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $HistoryRoot) {
    $profileRoot = [Environment]::GetEnvironmentVariable("USERPROFILE")
    if (-not $profileRoot) { $profileRoot = [Environment]::GetFolderPath("UserProfile") }
    $HistoryRoot = Join-Path $profileRoot "Desktop\RadioTEDU Play History"
}
if (-not $MirrorRoot) {
    $localAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
    if (-not $localAppData) { $localAppData = Join-Path $env:TEMP "RadioTEDU" }
    $MirrorRoot = Join-Path $localAppData "RadioTEDU\OnAir\PlayHistoryGitMirror"
}

$HistoryRoot = [IO.Path]::GetFullPath($HistoryRoot)
$MirrorRoot = [IO.Path]::GetFullPath($MirrorRoot)
$scriptRoot = Split-Path -Parent $PSScriptRoot
$exportScript = Join-Path $scriptRoot "scripts\export_play_history.py"

if (-not (Test-Path -LiteralPath $exportScript -PathType Leaf)) {
    throw "Export script not found: $exportScript"
}

# Refresh the CSVs immediately before the commit.  The app also runs this
# exporter in-process; this second pass makes the nightly job self-contained.
& py -3 $exportScript --db-path $DatabasePath --history-root $HistoryRoot | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Play-history export failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $HistoryRoot -PathType Container)) {
    throw "Play-history directory does not exist: $HistoryRoot"
}

New-Item -ItemType Directory -Force -Path $MirrorRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $MirrorRoot ".git") -PathType Container)) {
    & gh repo clone $GitHubRepository $MirrorRoot | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not clone $GitHubRepository" }
}

# Copy every report (including the preserved legacy folder) while leaving the
# mirror's .git directory untouched.  Files are never deleted from the mirror;
# this makes a transient Desktop permission failure recoverable on the next run.
Get-ChildItem -LiteralPath $HistoryRoot -File -Recurse | ForEach-Object {
    $relative = $_.FullName.Substring($HistoryRoot.Length).TrimStart('\', '/')
    $target = Join-Path $MirrorRoot $relative
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}

$readme = Join-Path $MirrorRoot "README.md"
if (-not (Test-Path -LiteralPath $readme -PathType Leaf)) {
    @"
# RadioTEDU OnAir Play History

This private repository is an automated nightly mirror of the RadioTEDU OnAir
immutable music-use ledger.  CSVs are generated on the broadcast computer and
include every station, artist, song title, source path, mount, UTC timestamp,
and cumulative play count.  The SQLite ledger and its hash-chain manifest are
the authoritative records; this repository is an off-machine backup.
"@ | Set-Content -LiteralPath $readme -Encoding UTF8
}

$gitStatus = & git -C $MirrorRoot status --porcelain
if ($gitStatus) {
    & git -C $MirrorRoot add --all
    if ($LASTEXITCODE -ne 0) { throw "git add failed" }
    $message = "Nightly play-history backup $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')"
    & git -C $MirrorRoot -c user.name="RadioTEDU OnAir Backup" -c user.email="onair-backup@radiotedu.local" commit -m $message | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
    $branch = (& git -C $MirrorRoot branch --show-current).Trim()
    if (-not $branch) { $branch = "main" }
    & git -C $MirrorRoot push origin ("HEAD:{0}" -f $branch) | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git push failed" }
}

Write-Output ("RadioTEDU play-history backup complete: {0}" -f $MirrorRoot)

