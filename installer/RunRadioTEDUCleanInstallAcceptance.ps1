[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [string]$InstallRoot = "",
    [int]$StartupTimeoutSec = 90,
    [switch]$KeepInstalled
)

$ErrorActionPreference = "Stop"
$serviceName = "RadioTEDU.OnAir.Supervisor"
$productExe = "RadioTEDU-OnAir.exe"
$backendExe = "RadioTEDU-OnAir-Backend.exe"
$supervisorExe = "RadioTEDU-OnAir-Supervisor.exe"
$dataRoot = Join-Path $env:ProgramData "RadioTEDU\OnAir"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "RunRadioTEDUCleanInstallAcceptance.ps1 must run from an elevated Administrator PowerShell."
}

$installerFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop))
if ([IO.Path]::GetFileName($installerFull) -notmatch '^RadioTEDU-OnAir-Setup-[0-9.]+\.exe$') {
    throw "Refusing a non-RadioTEDU installer: $installerFull"
}
$checksumFile = Join-Path (Split-Path -Parent $installerFull) (([IO.Path]::GetFileNameWithoutExtension($installerFull)) + ".sha256")
if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) {
    throw "Adjacent checksum file is required: $checksumFile"
}
$expectedHash = ((Get-Content -LiteralPath $checksumFile -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actualHash = (Get-FileHash -LiteralPath $installerFull -Algorithm SHA256).Hash.ToUpperInvariant()
if ($expectedHash -ne $actualHash) { throw "Installer checksum mismatch." }

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = Join-Path $env:TEMP ("RadioTEDU-OnAir-CleanInstall-" + [Guid]::NewGuid().ToString("N"))
}
$installFull = [IO.Path]::GetFullPath($InstallRoot)
$tempRoot = [IO.Path]::GetFullPath($env:TEMP)
if (-not $installFull.StartsWith($tempRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallRoot must be a child of the current user's TEMP directory."
}
if (Test-Path -LiteralPath $installFull) { throw "InstallRoot already exists; refusing to overwrite a pre-existing directory: $installFull" }
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) { throw "Service $serviceName already exists; run on a clean disposable VM." }
if (Test-Path -LiteralPath $dataRoot) { throw "ProgramData data root already exists; run on a clean disposable VM: $dataRoot" }

$installed = $false
try {
    $installerProcess = Start-Process -FilePath $installerFull -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS',"/DIR=$installFull","/TASKS=") -WindowStyle Hidden -Wait -PassThru
    if ($installerProcess.ExitCode -ne 0) { throw "Installer exited with code $($installerProcess.ExitCode)." }
    $installed = $true
    foreach ($relative in @($productExe, (Join-Path "backend" $backendExe), (Join-Path "supervisor" $supervisorExe))) {
        $path = Join-Path $installFull $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Installed artifact is missing: $path" }
    }
    $service = Get-Service -Name $serviceName -ErrorAction Stop
    if ($service.Status -notin @("Running", "StartPending")) { throw "Supervisor service is not running: $($service.Status)" }
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
    do {
        try { $response = Invoke-WebRequest -Uri "http://127.0.0.1:8100/api/health/live" -UseBasicParsing -TimeoutSec 4 } catch { $response = $null }
        if ($response -and [int]$response.StatusCode -eq 200) { Write-Output "PASS RadioTEDU clean-install artifacts, service, and loopback health verified."; return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Installed RadioTEDU backend did not become live on 127.0.0.1:8100 within $StartupTimeoutSec seconds."
}
finally {
    if (-not $KeepInstalled -and $installed) {
        $uninstaller = Join-Path $installFull "unins000.exe"
        if (Test-Path -LiteralPath $uninstaller) { Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -WindowStyle Hidden -Wait | Out-Null }
        if (Test-Path -LiteralPath $dataRoot) { Write-Output "ProgramData retained as designed: $dataRoot" }
    }
}
