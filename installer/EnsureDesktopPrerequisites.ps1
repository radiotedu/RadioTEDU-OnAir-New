[CmdletBinding()]
param(
    [scriptblock]$RuntimeInstalledCheck,
    [scriptblock]$DownloadAction,
    [scriptblock]$SignatureCheck,
    [string]$BootstrapperUrl = "https://go.microsoft.com/fwlink/?linkid=2124703",
    [string]$BootstrapperPath = (Join-Path $env:TEMP "RadioTEDU-OnAir-WebView2Bootstrapper.exe")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

$webView2RuntimeClientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

function Get-WebView2RuntimeRegistryPaths {
    $userPath = "Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\$webView2RuntimeClientId"
    if ([Environment]::Is64BitOperatingSystem) {
        return @(
            "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$webView2RuntimeClientId",
            $userPath
        )
    }
    return @(
        "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\EdgeUpdate\Clients\$webView2RuntimeClientId",
        $userPath
    )
}

function Test-WebView2RuntimeInstalled {
    if ($RuntimeInstalledCheck) {
        return [bool](& $RuntimeInstalledCheck)
    }
    foreach ($registryPath in Get-WebView2RuntimeRegistryPaths) {
        try {
            $versionText = [string](Get-ItemProperty -Path $registryPath -Name pv -ErrorAction Stop).pv
            if (-not [string]::IsNullOrWhiteSpace($versionText) -and
                ([version]$versionText) -gt [version]"0.0.0.0") {
                return $true
            }
        }
        catch {
        }
    }
    return $false
}

if (Test-WebView2RuntimeInstalled) {
    Write-Output "WebView2 runtime is present."
    return
}

$parent = Split-Path -Parent $BootstrapperPath
if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

if ($DownloadAction) {
    & $DownloadAction $BootstrapperUrl $BootstrapperPath
}
else {
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(90)
        $bytes = $client.GetByteArrayAsync($BootstrapperUrl).GetAwaiter().GetResult()
        [System.IO.File]::WriteAllBytes($BootstrapperPath, $bytes)
    }
    finally {
        $client.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $BootstrapperPath -PathType Leaf)) {
    throw "The Microsoft WebView2 bootstrapper could not be downloaded."
}

$signatureValid = if ($SignatureCheck) {
    [bool](& $SignatureCheck $BootstrapperPath)
}
else {
    $signature = Get-AuthenticodeSignature -FilePath $BootstrapperPath
    $signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid -and
        $signature.SignerCertificate.Subject -match "Microsoft"
}
if (-not $signatureValid) {
    throw "The WebView2 bootstrapper does not have a valid Microsoft signature."
}

$process = Start-Process -FilePath $BootstrapperPath -ArgumentList "/silent /install" -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0 -or -not (Test-WebView2RuntimeInstalled)) {
    throw "Microsoft WebView2 Runtime installation failed."
}

Write-Output "WebView2 runtime was installed."
