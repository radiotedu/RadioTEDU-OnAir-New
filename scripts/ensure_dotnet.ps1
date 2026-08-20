param(
    [string]$InstallDir,
    [string]$Version = "8.0.415",
    [string]$Architecture = "x64",
    [string]$InstallerUrl = "https://dot.net/v1/dotnet-install.ps1"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $InstallDir) {
    $InstallDir = Join-Path $root "..\.dotnet"
}

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$dotnetExe = Join-Path $InstallDir "dotnet.exe"
$installerScript = Join-Path $root "dotnet-install.ps1"

function Get-InstalledDotNetVersion {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath
    )

    if (-not (Test-Path $ExecutablePath)) {
        return ""
    }

    try {
        $version = & $ExecutablePath --version 2>$null
        if ($LASTEXITCODE -ne 0) {
            return ""
        }

        $candidate = @($version | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1)
        if ($candidate.Count -eq 0) {
            return ""
        }

        return $candidate[0].Trim()
    }
    catch {
        return ""
    }
}

$installedVersion = Get-InstalledDotNetVersion -ExecutablePath $dotnetExe
if ($installedVersion -eq $Version) {
    Write-Host "Using repo-local .NET SDK: $dotnetExe"
    Write-Output $dotnetExe
    exit 0
}
elseif ($installedVersion) {
    Write-Host "Existing repo-local .NET SDK version $installedVersion does not match requested $Version. Reinstalling."
}

if (-not (Test-Path $installerScript)) {
    Write-Host "Downloading official .NET installer script to $installerScript"
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $installerScript
    if (-not (Test-Path $installerScript)) {
        throw "Could not download dotnet-install.ps1 from $InstallerUrl."
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "Bootstrapping repo-local .NET SDK into $InstallDir"
& powershell -ExecutionPolicy Bypass -File $installerScript `
    -Version $Version `
    -InstallDir $InstallDir `
    -Architecture $Architecture | Out-Host

if ($LASTEXITCODE -ne 0) {
    throw "dotnet-install.ps1 failed while installing .NET $Version."
}

if (-not (Test-Path $dotnetExe)) {
    throw "dotnet.exe was not created at $dotnetExe."
}

$verifiedVersion = Get-InstalledDotNetVersion -ExecutablePath $dotnetExe
if ($verifiedVersion -ne $Version) {
    $reportedVersion = if ($verifiedVersion) { $verifiedVersion } else { "<unavailable>" }
    throw "Installed dotnet.exe at $dotnetExe reports $reportedVersion instead of requested version $Version."
}

Write-Host "Installed repo-local .NET SDK: $dotnetExe"
Write-Output $dotnetExe
