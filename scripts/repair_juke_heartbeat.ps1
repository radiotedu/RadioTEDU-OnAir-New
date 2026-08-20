[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LiveConfig,

    [Parameter(Mandatory = $true)]
    [string]$TrustedConfig,

    [switch]$RestartService
)

$ErrorActionPreference = 'Stop'

function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Configuration file not found: $Path"
    }

    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
            continue
        }

        $separator = $line.IndexOf('=')
        if ($separator -le 0) {
            continue
        }

        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim().Trim('"').Trim("'")
        $values[$name] = $value
    }

    return $values
}

function Set-DotEnvValues {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $separator = $line.IndexOf('=')
        if ($separator -gt 0) {
            $name = $line.Substring(0, $separator).Trim()
            if ($Values.ContainsKey($name)) {
                $lines.Add("$name=$($Values[$name])")
                $Values.Remove($name)
                continue
            }
        }

        $lines.Add($line)
    }

    foreach ($name in $Values.Keys) {
        $lines.Add("$name=$($Values[$name])")
    }

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllLines($Path, $lines, $encoding)
}

$trusted = Read-DotEnv -Path $TrustedConfig
$required = @('MEDIA_AGENT_HEARTBEAT_URL', 'MEDIA_AGENT_HEARTBEAT_SECRET')
foreach ($name in $required) {
    if (-not $trusted.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($trusted[$name])) {
        throw "Trusted configuration is missing $name"
    }
}

$heartbeatUri = [System.Uri]$trusted['MEDIA_AGENT_HEARTBEAT_URL']
if ($heartbeatUri.Scheme -ne 'https' -or $heartbeatUri.Host -match 'placeholder|web_server_lan_ip') {
    throw 'Trusted heartbeat URL must be a concrete HTTPS endpoint.'
}

$backupName = "juke-local.env.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$backupDirectory = Join-Path $env:LOCALAPPDATA 'RadioTEDU\OnAir\Backups'
[System.IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
$backupPath = Join-Path $backupDirectory $backupName
Copy-Item -LiteralPath $LiveConfig -Destination $backupPath -Force

$updates = @{
    MEDIA_AGENT_HEARTBEAT_URL = $trusted['MEDIA_AGENT_HEARTBEAT_URL']
    MEDIA_AGENT_HEARTBEAT_SECRET = $trusted['MEDIA_AGENT_HEARTBEAT_SECRET']
}
Set-DotEnvValues -Path $LiveConfig -Values $updates

Write-Output "Updated Juke heartbeat target and signing secret. Backup: $backupPath"

if ($RestartService) {
    $service = Get-Service -Name 'RadioTEDU.JukeLocalMediaAgent' -ErrorAction Stop
    Restart-Service -InputObject $service -Force -ErrorAction Stop
    $service.WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(30)
    )
    Write-Output 'RadioTEDU.JukeLocalMediaAgent restarted successfully.'
}
