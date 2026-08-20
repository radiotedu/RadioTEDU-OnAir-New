[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [int]$Port = 11154,
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script in an elevated PowerShell on the TinyIce server.'
    }
}

function Test-TinyIceHttp {
    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/")
    $request.Method = 'GET'
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    try {
        $response = $request.GetResponse()
        try {
            # TinyIce's hung state can return a TCP/HTTP header but no usable
            # body. Require a readable response byte, not merely a socket.
            $stream = $response.GetResponseStream()
            if ($null -eq $stream) { return $false }
            $buffer = New-Object byte[] 1
            return [int]$stream.Read($buffer, 0, 1) -gt 0
        }
        finally {
            $response.Close()
        }
    }
    catch [Net.WebException] {
        if ($null -ne $_.Exception.Response) { $_.Exception.Response.Close() }
        return $false
    }
    catch {
        return $false
    }
}

function Wait-TinyIceHttp {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-TinyIceHttp) { return $true }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-TinyIceServices {
    return @(
        Get-CimInstance Win32_Service | Where-Object {
            $_.Name -match '(?i)tiny.?ice' -or
            $_.DisplayName -match '(?i)tiny.?ice' -or
            $_.PathName -match '(?i)tiny.?ice'
        }
    )
}

function Get-TinyIceTasks {
    return @(
        Get-ScheduledTask | Where-Object {
            $_.TaskName -match '(?i)tiny.?ice' -or
            @($_.Actions | Where-Object {
                $_.Execute -match '(?i)tiny.?ice' -or
                $_.Arguments -match '(?i)tiny.?ice'
            }).Count -gt 0
        }
    )
}

Assert-Administrator

if (Test-TinyIceHttp) {
    [ordered]@{
        ok = $true
        action = 'none'
        reason = 'origin_already_responsive'
        port = $Port
    } | ConvertTo-Json -Compress
    exit 0
}

$services = @(Get-TinyIceServices)
$tasks = @(Get-TinyIceTasks)
if ($services.Count -gt 1 -or $tasks.Count -gt 1 -or
    ($services.Count -eq 1 -and $tasks.Count -eq 1)) {
    throw 'TinyIce ownership is ambiguous; no process, task, service, or host was restarted.'
}

$action = ''
$owner = ''
if ($services.Count -eq 1) {
    $service = $services[0]
    $action = 'restart_service'
    $owner = [string]$service.Name
    if ($PSCmdlet.ShouldProcess($owner, 'Restart only the TinyIce Windows service')) {
        Restart-Service -Name $owner -Force
    }
}
elseif ($tasks.Count -eq 1) {
    $task = $tasks[0]
    $action = 'restart_scheduled_task'
    $owner = [string]$task.TaskName
    if ($PSCmdlet.ShouldProcess($owner, 'Restart only the TinyIce scheduled task')) {
        Stop-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue
        Start-ScheduledTask -InputObject $task
    }
}
else {
    $processes = @(
        Get-CimInstance Win32_Process | Where-Object {
            $_.ExecutablePath -match '(?i)tiny.?ice' -or
            $_.CommandLine -match '(?i)tiny.?ice'
        }
    )
    if ($processes.Count -gt 0) {
        throw 'TinyIce is process-owned without a restartable service/task; no process was killed.'
    }
    throw 'No uniquely restartable TinyIce Windows service or scheduled task was found.'
}

if ($WhatIfPreference) { exit 0 }
if (-not (Wait-TinyIceHttp)) {
    throw "TinyIce $action completed but port $Port did not return HTTP within $TimeoutSeconds seconds."
}

[ordered]@{
    ok = $true
    action = $action
    owner = $owner
    port = $Port
    verified_http = $true
} | ConvertTo-Json -Compress
