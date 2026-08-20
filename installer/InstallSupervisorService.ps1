[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Prepare", "Start")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$AppRoot,

    [Parameter(Mandatory = $true)]
    [string]$DataRoot,

    [string]$ServiceName = "RadioTEDU.OnAir.Supervisor"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$productName = "RadioTEDU OnAir"
$supervisorFileName = "RadioTEDU-OnAir-Supervisor.exe"
$backendFileName = "RadioTEDU-OnAir-Backend.exe"
$backendPort = 8100
$supervisorPath = Join-Path $AppRoot "supervisor\$supervisorFileName"
$backendRoot = Join-Path $AppRoot "backend"
$backendPath = Join-Path $backendRoot $backendFileName
$servicesRoot = Join-Path $DataRoot "Services"
$configPath = Join-Path $servicesRoot "$ServiceName.services"
$serviceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"

function Invoke-ServiceControl {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & "$env:SystemRoot\System32\sc.exe" @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Windows service configuration failed."
    }
}

function Get-InstalledService {
    return Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
}

if ($Action -eq "Prepare") {
    foreach ($requiredPath in @($supervisorPath, $backendPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required $productName executable is missing: $requiredPath"
        }
    }

    New-Item -ItemType Directory -Force -Path $servicesRoot | Out-Null
    $definition = "backend|$backendPath|--host 127.0.0.1 --port $backendPort|$backendRoot|true"
    [System.IO.File]::WriteAllText(
        $configPath,
        "# $productName supervised process definitions`r`n$definition`r`n",
        [System.Text.UTF8Encoding]::new($false))

    $service = Get-InstalledService
    if ($null -ne $service -and $service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        Stop-Service -Name $ServiceName -Force
        $service.WaitForStatus(
            [System.ServiceProcess.ServiceControllerStatus]::Stopped,
            [TimeSpan]::FromSeconds(30))
    }

    $binaryPath = '"' + $supervisorPath + '" --service-name ' + $ServiceName + ' --config "' + $configPath + '"'
    $account = "NT SERVICE\$ServiceName"
    if ($null -eq $service) {
        Invoke-ServiceControl create $ServiceName "binPath= $binaryPath" "start= auto" "obj= $account" "DisplayName= $productName Supervisor"
    }
    else {
        Invoke-ServiceControl config $ServiceName "binPath= $binaryPath" "start= auto" "obj= $account"
    }

    Invoke-ServiceControl description $ServiceName "Supervises the local $productName backend and isolated station workers."
    Invoke-ServiceControl sidtype $ServiceName unrestricted
    Invoke-ServiceControl failure $ServiceName "reset= 86400" "actions= restart/5000/restart/15000/restart/60000"
    Invoke-ServiceControl failureflag $ServiceName 1

    if (-not (Test-Path -LiteralPath $serviceRegistryPath)) {
        throw "The $productName supervisor service registry key is missing."
    }
    $serviceEnvironment = [string[]]@(
        "CLEANROOM_DATA_ROOT=$DataRoot",
        "CLEANROOM_DB_PATH=$(Join-Path $DataRoot 'cleanroom.db')",
        "CLEANROOM_USER_CONFIG_ROOT=$DataRoot",
        "CLEANROOM_JWT_SECRET_FILE=$(Join-Path $DataRoot 'secrets\jwt-signing.key')",
        "CLEANROOM_TOOLS_DIR=$(Join-Path $backendRoot 'tools')",
        "CLEANROOM_OPEN_PANEL=0",
        "CLEANROOM_SKIP_STARTUP_AI=1",
        "CLEANROOM_SKIP_ICECAST_METADATA=1"
    )
    New-ItemProperty -LiteralPath $serviceRegistryPath -Name "Environment" `
        -PropertyType MultiString -Value $serviceEnvironment -Force | Out-Null
    return
}

$installed = Get-InstalledService
if ($null -eq $installed) {
    throw "The $productName supervisor service is not installed."
}
if ($installed.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Running) {
    Start-Service -Name $ServiceName
    $installed.WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(30))
}
