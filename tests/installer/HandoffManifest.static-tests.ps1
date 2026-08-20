$ErrorActionPreference = 'Stop'
$generatorPath = Join-Path $PSScriptRoot '..\..\installer\NewBroadcastPcHandoffManifest.ps1'
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($generatorPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw "Manifest generator has parse errors: $($errors.Count)" }
$source = [IO.File]::ReadAllText($generatorPath)
foreach ($required in @('schemaVersion', 'SHA-256', 'Get-FileHash', 'ValidateOnly', 'Reparse points', 'node_modules', '.venv', 'Duplicate or case-colliding')) {
    if (-not $source.Contains($required)) { throw "Manifest generator is missing required control: $required" }
}
Write-Output 'Handoff manifest static tests passed.'
