param(
    [int]$Port = 18110,
    [ValidateSet("Auto", "Source", "Packaged")]
    [string]$Mode = "Auto",
    [ValidateRange(5, 300)]
    [int]$ReadinessTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$runRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $runRoot "..\.."))
$canonicalLauncher = Join-Path $repoRoot "tools\start_radio_backend.ps1"
if (-not (Test-Path -LiteralPath $canonicalLauncher -PathType Leaf)) {
    throw "The durable RadioTEDU OnAir launcher is missing: $canonicalLauncher"
}

& $canonicalLauncher `
    -Port $Port `
    -Mode $Mode `
    -ReadinessTimeoutSeconds $ReadinessTimeoutSeconds
