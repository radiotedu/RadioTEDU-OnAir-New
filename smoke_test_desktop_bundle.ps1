param(
    [string]$Root = $PSScriptRoot,
    [string]$InstallerPath = "",
    [string]$InstallerPathFile = "release\setup\last_setup_path.txt",
    [string]$BackendPath = "",
    [string]$BackendPathFile = "last_build_path.txt",
    [int]$BackendPort = 0,
    [int]$HealthTimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$BackendExeName = "RadioTEDU-OnAir-Backend.exe"
$ShellExeName = "RadioTEDU-OnAir.exe"

function Resolve-ProjectRoot {
    param([Parameter(Mandatory = $true)][string]$ScriptRoot)

    return [System.IO.Path]::GetFullPath($ScriptRoot)
}

function Resolve-FullPath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$PathValue
    )

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $BasePath $PathValue))
}

function Assert-FileExists {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function Assert-RadioTeduIcon {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Add-Type -AssemblyName System.Drawing
    $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($ExecutablePath)
    if (-not $icon) {
        throw "$Label does not contain an extractable application icon: $ExecutablePath"
    }

    $bitmap = $null
    try {
        $bitmap = $icon.ToBitmap()
        $radioTeduRedPixels = 0
        for ($y = 0; $y -lt $bitmap.Height; $y++) {
            for ($x = 0; $x -lt $bitmap.Width; $x++) {
                $pixel = $bitmap.GetPixel($x, $y)
                if (
                    $pixel.A -gt 100 -and
                    $pixel.R -gt 170 -and
                    $pixel.R -gt ($pixel.G * 1.35) -and
                    $pixel.R -gt ($pixel.B * 1.35)
                ) {
                    $radioTeduRedPixels++
                }
            }
        }

        # The 32x32 RadioTEDU mark contains well over 100 red pixels. Keep a
        # deliberately conservative floor so resampling differences do not
        # cause false failures, while the generic Windows EXE icon (zero red
        # pixels) can never pass a release smoke check.
        if ($radioTeduRedPixels -lt 20) {
            throw "$Label is missing the embedded RadioTEDU logo: $ExecutablePath"
        }
    }
    finally {
        if ($bitmap) {
            $bitmap.Dispose()
        }
        $icon.Dispose()
    }
}

function Resolve-RecordedInstallerPath {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$InstallerPathFile
    )

    $markerPath = Resolve-FullPath -BasePath $ProjectRoot -PathValue $InstallerPathFile
    Assert-FileExists -Path $markerPath -Label "Installer marker"

    $recordedPath = Get-Content -Path $markerPath -ErrorAction Stop | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace([string]$recordedPath)) {
        throw "Installer marker is empty: $markerPath"
    }

    $recordedPath = ([string]$recordedPath).Trim()
    if (-not [System.IO.Path]::IsPathRooted($recordedPath)) {
        $recordedPath = Resolve-FullPath -BasePath (Split-Path -Parent $markerPath) -PathValue $recordedPath
    }

    return [System.IO.Path]::GetFullPath($recordedPath)
}

function Resolve-RecordedBackendPath {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$BackendPathFile
    )

    $markerPath = Resolve-FullPath -BasePath $ProjectRoot -PathValue $BackendPathFile
    Assert-FileExists -Path $markerPath -Label "Backend marker"

    $recordedPath = Get-Content -Path $markerPath -ErrorAction Stop | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace([string]$recordedPath)) {
        throw "Backend marker is empty: $markerPath"
    }

    $recordedPath = ([string]$recordedPath).Trim()
    if (-not [System.IO.Path]::IsPathRooted($recordedPath)) {
        $recordedPath = Resolve-FullPath -BasePath (Split-Path -Parent $markerPath) -PathValue $recordedPath
    }

    return [System.IO.Path]::GetFullPath($recordedPath)
}

function Test-ProcessExited {
    param([Parameter(Mandatory = $true)]$Process)

    $property = $Process.PSObject.Properties.Match("HasExited")
    if ($property.Count -gt 0) {
        return [bool]$Process.HasExited
    }

    return $false
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return (($listener.LocalEndpoint).Port)
    }
    finally {
        $listener.Stop()
    }
}

function Resolve-BackendSmokePort {
    param([Parameter(Mandatory = $true)][int]$BackendPort)

    if ($BackendPort -gt 0) {
        return $BackendPort
    }

    return Get-FreeTcpPort
}

function Resolve-SmokeToolsDir {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    return Join-Path $ProjectRoot "build\desktop-smoke-tools"
}

function Resolve-SmokeDbPath {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    return Join-Path $ProjectRoot "build\desktop-smoke\cleanroom-smoke.db"
}

function New-SmokeWorkspace {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $workspaceRoot = Join-Path $ProjectRoot ("build\desktop-smoke\run-" + [Guid]::NewGuid().ToString("N"))
    return [pscustomobject]@{
        Root = $workspaceRoot
        ToolsDir = (Join-Path $workspaceRoot "tools")
        DatabasePath = (Join-Path $workspaceRoot "cleanroom-smoke.db")
    }
}

function Seed-SmokeToolsDir {
    param([Parameter(Mandatory = $true)][string]$ToolsDir)

    $binDir = Join-Path $ToolsDir "bin"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null

    $ytDlpSource = @(
        (Get-Command yt-dlp.exe -ErrorAction SilentlyContinue).Source,
        (Get-Command yt-dlp -ErrorAction SilentlyContinue).Source
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

    if ($ytDlpSource) {
        Copy-Item -Path $ytDlpSource -Destination (Join-Path $binDir "yt-dlp.exe") -Force
    }

    return $binDir
}

function Assert-TcpPortAvailable {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    $started = $false
    try {
        $listener.Start()
        $started = $true
    }
    catch {
        throw "Backend port is already in use: $Port"
    }
    finally {
        if ($started) {
            $listener.Stop()
        }
    }
}

function Start-BackendProcess {
    param(
        [Parameter(Mandatory = $true)][string]$BackendPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][int]$BackendPort,
        [Parameter(Mandatory = $true)][string]$ToolsDir,
        [Parameter(Mandatory = $true)][string]$DatabasePath
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $BackendPath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $runtimeRoot = Split-Path -Parent $DatabasePath
    $startInfo.EnvironmentVariables["CLEANROOM_OPEN_PANEL"] = "0"
    $startInfo.EnvironmentVariables["CLEANROOM_HOST"] = "127.0.0.1"
    $startInfo.EnvironmentVariables["CLEANROOM_PORT"] = $BackendPort.ToString()
    $startInfo.EnvironmentVariables["CLEANROOM_TOOLS_DIR"] = $ToolsDir
    $startInfo.EnvironmentVariables["CLEANROOM_DB_PATH"] = $DatabasePath
    $startInfo.EnvironmentVariables["CLEANROOM_DATA_ROOT"] = $runtimeRoot
    $startInfo.EnvironmentVariables["CLEANROOM_USER_CONFIG_ROOT"] = (Join-Path $runtimeRoot "config")
    $startInfo.EnvironmentVariables["CLEANROOM_DISABLE_LIBRARY_WATCHER"] = "1"
    $startInfo.EnvironmentVariables["CLEANROOM_SKIP_STARTUP_AI"] = "1"
    $startInfo.EnvironmentVariables["CLEANROOM_SKIP_WORKER_AUTOSTART"] = "1"

    return [System.Diagnostics.Process]::Start($startInfo)
}

function Get-BackendProcessTreeIds {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($RootProcessId)
    [void]$seen.Add($RootProcessId)

    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId" -ErrorAction SilentlyContinue)
        foreach ($child in $children) {
            $childId = [int]$child.ProcessId
            if ($seen.Add($childId)) {
                $queue.Enqueue($childId)
            }
        }
    }

    return @($seen)
}

function Stop-BackendProcessTree {
    param([Parameter(Mandatory = $true)]$RootProcess)

    $rootId = 0
    try {
        $rootId = [int]$RootProcess.Id
    }
    catch {
        return
    }

    $processIds = @(Get-BackendProcessTreeIds -RootProcessId $rootId | Sort-Object -Descending)
    foreach ($processId in $processIds) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
        catch {
        }
    }
}

function Invoke-BackendSmoke {
    param(
        [Parameter(Mandatory = $true)][string]$BackendPath,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][int]$BackendPort,
        [Parameter(Mandatory = $true)][int]$HealthTimeoutSec
    )

    Assert-FileExists -Path $BackendPath -Label "Backend artifact"
    $workspace = New-SmokeWorkspace -ProjectRoot $ProjectRoot
    try {
        $maxAttempts = if ($BackendPort -le 0) { 10 } else { 1 }
        $perAttemptTimeoutSec = if ($BackendPort -le 0) { [Math]::Min($HealthTimeoutSec, 15) } else { $HealthTimeoutSec }
        $attempt = 0
        $lastError = $null
        $toolsDir = $workspace.ToolsDir
        $databasePath = $workspace.DatabasePath
        Seed-SmokeToolsDir -ToolsDir $toolsDir | Out-Null
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $databasePath) | Out-Null

        while ($attempt -lt $maxAttempts) {
            $attempt++
            $selectedPort = Resolve-BackendSmokePort -BackendPort $BackendPort
            $backendDirectory = Split-Path -Parent $BackendPath
            $backendProcess = $null
            $healthy = $false
            # The desktop bundle gate proves the packaged process can start and own
            # its isolated port. Full readiness (including disk reserve policy) is
            # validated by smoke_test_backend_onefile.ps1.
            $healthUri = "http://127.0.0.1:$selectedPort/api/health/live"

            try {
                if ($BackendPort -gt 0) {
                    Assert-TcpPortAvailable -Port $selectedPort
                }

                $backendProcess = Start-BackendProcess `
                    -BackendPath $BackendPath `
                    -WorkingDirectory $backendDirectory `
                    -BackendPort $selectedPort `
                    -ToolsDir $toolsDir `
                    -DatabasePath $databasePath

                if (-not $backendProcess) {
                    throw "Backend process could not be started: $BackendPath"
                }

                $deadline = (Get-Date).AddSeconds($perAttemptTimeoutSec)
                while ((Get-Date) -lt $deadline) {
                    try {
                        Invoke-RestMethod -Uri $healthUri -Method Get -TimeoutSec 5 | Out-Null
                        if (Test-ProcessExited -Process $backendProcess) {
                            throw "Backend process exited before health ownership was confirmed: $BackendPath"
                        }
                        $healthy = $true
                        break
                    }
                    catch {
                        if (Test-ProcessExited -Process $backendProcess) {
                            throw "Backend exited before liveness was confirmed: $BackendPath"
                        }
                        Start-Sleep -Milliseconds 250
                    }
                }

                if (-not $healthy) {
                    throw "Backend health check timed out: $healthUri"
                }

                return $selectedPort
            }
            catch {
                $lastError = $_
                if ($BackendPort -gt 0 -or $attempt -ge $maxAttempts) {
                    throw
                }
            }
            finally {
                if ($backendProcess -and -not (Test-ProcessExited -Process $backendProcess)) {
                    Stop-BackendProcessTree -RootProcess $backendProcess
                }
            }
        }

        if ($lastError) {
            throw $lastError
        }

        throw "Backend smoke did not complete."
    }
    finally {
        if ($workspace -and (Test-Path $workspace.Root)) {
            Remove-Item -Path $workspace.Root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$projectRoot = Resolve-ProjectRoot -ScriptRoot $Root
$backendPath = if ([string]::IsNullOrWhiteSpace($BackendPath)) {
    Resolve-RecordedBackendPath -ProjectRoot $projectRoot -BackendPathFile $BackendPathFile
}
else {
    Resolve-FullPath -BasePath $projectRoot -PathValue $BackendPath
}

$shellPath = Join-Path $projectRoot "dist\desktop\shell\$ShellExeName"

Assert-FileExists -Path $backendPath -Label "Backend artifact"
Assert-FileExists -Path $shellPath -Label "Desktop shell artifact"

if ([string]::IsNullOrWhiteSpace($InstallerPath)) {
    $InstallerPath = Resolve-RecordedInstallerPath -ProjectRoot $projectRoot -InstallerPathFile $InstallerPathFile
}
else {
$InstallerPath = Resolve-FullPath -BasePath $projectRoot -PathValue $InstallerPath
}

Assert-FileExists -Path $InstallerPath -Label "Installer artifact"
$selectedBackendPort = Invoke-BackendSmoke -BackendPath $backendPath -ProjectRoot $projectRoot -BackendPort $BackendPort -HealthTimeoutSec $HealthTimeoutSec
Assert-RadioTeduIcon -ExecutablePath $shellPath -Label "Desktop shell artifact"

Write-Host "Desktop bundle smoke check passed."
Write-Host "Backend artifact: $backendPath"
Write-Host "Desktop shell artifact: $shellPath"
Write-Host "Installer artifact: $InstallerPath"
Write-Host "Backend health port: $selectedBackendPort"
