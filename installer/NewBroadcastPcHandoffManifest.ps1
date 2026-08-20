#Requires -Version 5.1
<#
.SYNOPSIS
Creates or verifies the deterministic non-secret RadioTEDU handoff manifest.

.DESCRIPTION
The manifest contains normalized relative paths, byte sizes and SHA-256 digests
for deployable non-secret handoff inputs. It deliberately excludes all secret
files and reproducible/generated output; see the provisioning runbook.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$HandoffRoot,
    [string]$ManifestPath,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$schemaVersion = 1
$excludedDirectoryNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
@('secrets', 'node_modules', '.venv', 'dist', 'build', '.cache', '__pycache__', '.pytest_cache', '.mypy_cache', 'coverage', '.next', '.turbo', 'logs') | ForEach-Object { [void]$excludedDirectoryNames.Add($_) }

function Fail([string]$Message) { throw "Handoff manifest blocked: $Message" }
function Normalize-RelativePath([string]$Root, [string]$Path) {
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($fullRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { Fail 'A handoff path is outside the root.' }
    $relative = $fullPath.Substring($fullRoot.Length).TrimStart('\').Replace('\', '/')
    if ($relative.Length -eq 0 -or $relative.StartsWith('../') -or $relative.Contains('//')) { Fail 'A handoff path cannot be normalized safely.' }
    return $relative
}
function Test-ExcludedFile([string]$RelativePath, [string]$ManifestRelativePath) {
    if ([string]::Equals($RelativePath, $ManifestRelativePath, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $name = [IO.Path]::GetFileName($RelativePath)
    if ($name -match '(?i)^\.env(?:\..*)?$|\.env(?:\..*)?$|\.(pem|key|pfx|p12|dpapi)$|credential|secret') { return $true }
    return $false
}
function Get-ManifestEntries([string]$Root, [string]$ManifestFullPath) {
    $manifestRelative = Normalize-RelativePath $Root $ManifestFullPath
    $entries = [Collections.Generic.List[object]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    function Visit([string]$Directory) {
        foreach ($item in Get-ChildItem -LiteralPath $Directory -Force -ErrorAction Stop) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'Reparse points are not permitted in a handoff manifest.' }
            if ($item.PSIsContainer) {
                if ($excludedDirectoryNames.Contains($item.Name)) { continue }
                Visit $item.FullName
                continue
            }
            $relative = Normalize-RelativePath $Root $item.FullName
            if (Test-ExcludedFile $relative $manifestRelative) { continue }
            if (-not $seen.Add($relative)) { Fail 'Duplicate or case-colliding handoff path.' }
            $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash.ToLowerInvariant()
            $entries.Add([ordered]@{ path = $relative; size = [int64]$item.Length; sha256 = $digest })
        }
    }
    Visit $Root
    return @($entries | Sort-Object { $_.path })
}
function Read-Manifest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail 'Handoff manifest is missing.' }
    try { $document = [IO.File]::ReadAllText($Path) | ConvertFrom-Json -ErrorAction Stop } catch { Fail 'Handoff manifest is not valid JSON.' }
    if ($document.schemaVersion -ne $schemaVersion -or $document.algorithm -ne 'SHA-256' -or $null -eq $document.files) { Fail 'Handoff manifest has an unsupported schema.' }
    return $document
}
function Assert-ManifestMatches([string]$Root, [string]$Path) {
    $document = Read-Manifest $Path
    $actual = Get-ManifestEntries $Root ([IO.Path]::GetFullPath($Path))
    $expected = @($document.files)
    if ($expected.Count -ne $actual.Count) { Fail 'Handoff manifest has missing or extra files.' }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    for ($index = 0; $index -lt $expected.Count; $index++) {
        $entry = $expected[$index]
        if ($null -eq $entry.path -or $null -eq $entry.sha256 -or $null -eq $entry.size) { Fail 'Handoff manifest has an invalid file entry.' }
        $path = [string]$entry.path
        if ($path -ne (Normalize-RelativePath $Root (Join-Path $Root $path.Replace('/', '\')))) { Fail 'Handoff manifest contains a non-normalized path.' }
        if (-not $seen.Add($path)) { Fail 'Handoff manifest contains duplicate or case-colliding paths.' }
        if ($path -ne $actual[$index].path -or [int64]$entry.size -ne [int64]$actual[$index].size -or -not [string]::Equals([string]$entry.sha256, [string]$actual[$index].sha256, [StringComparison]::OrdinalIgnoreCase)) {
            Fail 'Handoff manifest has a missing, extra, size-mismatched, or hash-mismatched file.'
        }
    }
}

$HandoffRoot = [IO.Path]::GetFullPath($HandoffRoot)
if (-not (Test-Path -LiteralPath $HandoffRoot -PathType Container)) { Fail 'Handoff root is missing.' }
if ([string]::IsNullOrWhiteSpace($ManifestPath)) { $ManifestPath = Join-Path $HandoffRoot 'handoff-manifest.json' }
$ManifestPath = [IO.Path]::GetFullPath($ManifestPath)
if (-not $ManifestPath.StartsWith($HandoffRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { Fail 'Manifest must be located inside the handoff root.' }
if ($ValidateOnly) {
    Assert-ManifestMatches $HandoffRoot $ManifestPath
    Write-Output 'Handoff manifest validation passed.'
    return
}
$files = Get-ManifestEntries $HandoffRoot $ManifestPath
$document = [ordered]@{ schemaVersion = $schemaVersion; algorithm = 'SHA-256'; files = $files }
$temporary = "$ManifestPath.$([Guid]::NewGuid().ToString('N')).tmp"
try {
    [IO.File]::WriteAllText($temporary, ($document | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $ManifestPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
Write-Output 'Handoff manifest generated.'
