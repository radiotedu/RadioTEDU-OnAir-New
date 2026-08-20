[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Parameter(Mandatory = $true)]
    [string]$DataRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$resolvedRoot = [System.IO.Path]::GetFullPath($DataRoot)
$dumpRoot = Join-Path $resolvedRoot "CrashDumps"
[System.IO.Directory]::CreateDirectory($dumpRoot) | Out-Null

$executables = @(
    "RadioTEDU-OnAir.exe",
    "RadioTEDU-OnAir-Backend.exe",
    "RadioTEDU-OnAir-Supervisor.exe"
)

$localDumpsRoot = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
foreach ($executable in $executables) {
    $key = Join-Path $localDumpsRoot $executable
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name "DumpFolder" -PropertyType ExpandString -Value $dumpRoot -Force | Out-Null
    New-ItemProperty -Path $key -Name "DumpCount" -PropertyType DWord -Value 10 -Force | Out-Null
    # Mini dumps preserve exception, thread, module, and stack evidence without
    # allowing repeated full-process dumps to exhaust a broadcast PC's disk.
    New-ItemProperty -Path $key -Name "DumpType" -PropertyType DWord -Value 1 -Force | Out-Null
}

Write-Output "Configured bounded LocalDumps for $($executables.Count) RadioTEDU OnAir executables."
