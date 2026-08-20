param(
    [int]$Port = 18110,
    [ValidateSet("Auto", "Source", "Packaged")]
    [string]$Mode = "Auto",
    [ValidateRange(5, 300)]
    [int]$ReadinessTimeoutSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runRoot = Join-Path $env:PROGRAMDATA "RadioTEDU\OnAir"
$dataRoot = $runRoot
$userRoot = $runRoot
$logsRoot = Join-Path $runRoot "logs"
$sourceEntrypoint = Join-Path $repoRoot "run_cleanroom.py"
$lastBuildPath = Join-Path $repoRoot "last_build_path.txt"

function Resolve-PackagedBackend {
    $candidates = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $lastBuildPath -PathType Leaf) {
        $recorded = (Get-Content -LiteralPath $lastBuildPath -Raw).Trim()
        if ($recorded) {
            $candidates.Add($recorded)
        }
    }
    $candidates.Add((Join-Path $repoRoot "dist\backend\RadioTEDU-OnAir-Backend.exe"))

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Resolve-SourcePython {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:RADIOTEDU_SOURCE_PYTHON) {
        $candidates.Add($env:RADIOTEDU_SOURCE_PYTHON)
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand -and $pythonCommand.Source) {
        $candidates.Add($pythonCommand.Source)
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Resolve-ToolsRoot([string]$PackagedBackend) {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($PackagedBackend) {
        $candidates.Add((Join-Path (Split-Path -Parent $PackagedBackend) "tools"))
    }
    $candidates.Add((Join-Path $repoRoot "dist\backend\tools"))

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate "bin\ffmpeg.exe") -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Test-RadioBackendLive {
    try {
        $response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$Port/api/health/live" `
            -Method Get `
            -TimeoutSec 2
        return ([string]$response.service -eq "radiotedu-onair" -and [string]$response.status -eq "ok")
    } catch {
        return $false
    }
}

function Wait-RadioBackendLive(
    [System.Diagnostics.Process]$Process,
    [int]$TimeoutSeconds
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $Process.Refresh()
        if ($Process.HasExited) {
            return $false
        }
        if (Test-RadioBackendLive) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Start-RadioBackendProcess(
    [ValidateSet("Source", "Packaged")]
    [string]$SelectedMode,
    [string]$PythonExecutable,
    [string]$PackagedBackend
) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutPath = Join-Path $logsRoot "backend-$stamp-$($SelectedMode.ToLowerInvariant()).stdout.log"
    $stderrPath = Join-Path $logsRoot "backend-$stamp-$($SelectedMode.ToLowerInvariant()).stderr.log"

    if ($SelectedMode -eq "Source") {
        $process = Start-Process `
            -FilePath $PythonExecutable `
            -ArgumentList @($sourceEntrypoint) `
            -WorkingDirectory $repoRoot `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $executable = $PythonExecutable
    } else {
        $packagedRoot = Split-Path -Parent $PackagedBackend
        $process = Start-Process `
            -FilePath $PackagedBackend `
            -WorkingDirectory $packagedRoot `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $executable = $PackagedBackend
    }

    [pscustomobject]@{
        Process = $process
        Executable = $executable
        Stdout = $stdoutPath
        Stderr = $stderrPath
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $dataRoot "cleanroom.db") -PathType Leaf)) {
    throw "Migrated database is missing: $(Join-Path $dataRoot 'cleanroom.db')"
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    if (Test-RadioBackendLive) {
        throw "RadioTEDU OnAir is already running on port $Port."
    }
    throw "Port $Port is already in use by another program. RadioTEDU OnAir was not changed."
}

$packagedBackend = Resolve-PackagedBackend
$pythonExecutable = Resolve-SourcePython
$sourceAvailable = (
    $null -ne $pythonExecutable -and
    (Test-Path -LiteralPath $sourceEntrypoint -PathType Leaf)
)

$selectedMode = $Mode
if ($selectedMode -eq "Auto") {
    $selectedMode = if ($sourceAvailable) { "Source" } else { "Packaged" }
}
if ($selectedMode -eq "Source" -and -not $sourceAvailable) {
    throw "The verified source runtime is unavailable; Python 3.12 and run_cleanroom.py are required."
}
if ($selectedMode -eq "Packaged" -and -not $packagedBackend) {
    throw "No packaged RadioTEDU OnAir backend is available."
}

$toolsRoot = Resolve-ToolsRoot -PackagedBackend $packagedBackend
if (-not $toolsRoot) {
    throw "The managed FFmpeg tools directory is unavailable."
}

New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null
$env:CLEANROOM_PORT = [string]$Port
$env:CLEANROOM_DB_PATH = Join-Path $dataRoot "cleanroom.db"
$env:CLEANROOM_DATA_ROOT = $dataRoot
$env:CLEANROOM_TOOLS_DIR = $toolsRoot
$env:CLEANROOM_USER_CONFIG_ROOT = $userRoot
$env:CLEANROOM_JWT_SECRET_FILE = Join-Path $userRoot "secrets\jwt-signing.key"
$env:CLEANROOM_CREDENTIAL_STORE_FILE = Join-Path $userRoot "secrets\station-credentials.json"
$env:CLEANROOM_CREDENTIAL_DPAPI_SCOPE = "machine"
$env:CLEANROOM_OPEN_PANEL = "0"
$env:CLEANROOM_SKIP_STARTUP_AI = "1"
$env:CLEANROOM_SKIP_ICECAST_METADATA = "1"
$env:RADIOTEDU_PROCESS_ISOLATED_WORKERS = "1"

$launch = Start-RadioBackendProcess `
    -SelectedMode $selectedMode `
    -PythonExecutable $pythonExecutable `
    -PackagedBackend $packagedBackend

if (-not (Wait-RadioBackendLive -Process $launch.Process -TimeoutSeconds $ReadinessTimeoutSeconds)) {
    if (-not $launch.Process.HasExited) {
        Stop-Process -Id $launch.Process.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $launch.Process.Id -Timeout 10 -ErrorAction SilentlyContinue
    }

    if ($selectedMode -ne "Source" -or -not $packagedBackend) {
        throw "RadioTEDU OnAir $selectedMode backend did not become ready. See $($launch.Stderr)."
    }

    $selectedMode = "Packaged"
    $launch = Start-RadioBackendProcess `
        -SelectedMode $selectedMode `
        -PythonExecutable $pythonExecutable `
        -PackagedBackend $packagedBackend
    if (-not (Wait-RadioBackendLive -Process $launch.Process -TimeoutSeconds $ReadinessTimeoutSeconds)) {
        throw "Neither the source hotfix nor packaged fallback became ready. See $($launch.Stderr)."
    }
}

Set-Content -LiteralPath (Join-Path $runRoot "backend.pid") -Value $launch.Process.Id -Encoding ASCII
[pscustomobject]@{
    pid = $launch.Process.Id
    port = $Port
    mode = $selectedMode.ToLowerInvariant()
    executable = $launch.Executable
    database = $env:CLEANROOM_DB_PATH
    tools = $env:CLEANROOM_TOOLS_DIR
    stdout = $launch.Stdout
    stderr = $launch.Stderr
} | ConvertTo-Json -Compress
