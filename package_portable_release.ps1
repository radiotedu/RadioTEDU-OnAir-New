param(
    [string]$ExePath = ".\dist\backend\RadioTEDU-OnAir-Backend.exe",
    [string]$ReleaseRoot = ".\release",
    [string]$LastReleasePathFile = ".\last_release_path.txt",
    [switch]$SkipBuild,
    [switch]$OpenFolder
)

$ErrorActionPreference = "Stop"
$BackendExeName = "RadioTEDU-OnAir-Backend.exe"
$LegacyBackendExeName = "cleanroom-radio-backend.exe"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Resolve-FullPath {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue
    )

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $root $PathValue))
}

function Resolve-ExecutablePath {
    param(
        [Parameter(Mandatory = $true)][string]$RequestedPath
    )

    $requestedCandidate = Resolve-FullPath -PathValue $RequestedPath
    if (Test-Path $requestedCandidate) {
        return $requestedCandidate
    }

    $lastBuildPathFile = Join-Path $root "last_build_path.txt"
    if (Test-Path $lastBuildPathFile) {
        $lastBuiltExe = (Get-Content -Path $lastBuildPathFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($lastBuiltExe -and (Test-Path $lastBuiltExe)) {
            Write-Warning "Requested EXE not found. Using last build path from $lastBuildPathFile"
            return [System.IO.Path]::GetFullPath($lastBuiltExe)
        }
    }

    $backendDir = Join-Path $root "dist\backend\$BackendExeName"
    if (Test-Path $backendDir) {
        return [System.IO.Path]::GetFullPath($backendDir)
    }

    $legacyDistExe = Join-Path $root "dist\$LegacyBackendExeName"
    if (Test-Path $legacyDistExe) {
        Write-Warning "Using legacy dist root EXE path: $legacyDistExe"
        return [System.IO.Path]::GetFullPath($legacyDistExe)
    }

    $candidatePaths = @()
    $distRoot = Join-Path $root "dist"
    if (Test-Path $distRoot) {
        foreach ($exeName in @($BackendExeName, $LegacyBackendExeName)) {
            $candidatePaths += Join-Path $distRoot "backend\$exeName"
            $candidatePaths += Join-Path $distRoot $exeName
            $candidatePaths += Get-ChildItem -Path $distRoot -Directory -Filter "backend*" -ErrorAction SilentlyContinue `
                | ForEach-Object { Join-Path $_.FullName $exeName }
        }
    }

    $candidatePaths += Get-ChildItem -Path $root -Directory -Filter "dist*" -ErrorAction SilentlyContinue `
        | Where-Object { $_.FullName -ne $distRoot } `
        | ForEach-Object {
            foreach ($exeName in @($BackendExeName, $LegacyBackendExeName)) {
                @(
                    Join-Path $_.FullName "backend\$exeName"
                    Join-Path $_.FullName $exeName
                    Get-ChildItem -Path $_.FullName -Directory -Filter "backend*" -ErrorAction SilentlyContinue `
                        | ForEach-Object { Join-Path $_.FullName $exeName }
                )
            }
        }

    $candidates = $candidatePaths | Where-Object { $_ -and (Test-Path $_) } | ForEach-Object {
        [pscustomobject]@{
            Path = $_
            LastWriteTime = (Get-Item $_).LastWriteTimeUtc
        }
    } | Sort-Object LastWriteTime -Descending

    if ($candidates -and @($candidates).Count -gt 0) {
        $candidate = @($candidates)[0].Path
        Write-Warning "Requested EXE not found. Using most recent build: $candidate"
        return [System.IO.Path]::GetFullPath($candidate)
    }

    throw "Executable not found: $requestedCandidate"
}

$requestedExeCandidate = Resolve-FullPath -PathValue $ExePath
if ((-not $SkipBuild) -and (-not (Test-Path $requestedExeCandidate))) {
    $buildScript = Join-Path $root "build_backend_onefile.ps1"
    Write-Output "EXE not found. Building fresh package via $buildScript"
    & powershell -ExecutionPolicy Bypass -File $buildScript
    if ($LASTEXITCODE -ne 0) {
        throw "Portable release build failed."
    }
}

$exeFull = Resolve-ExecutablePath -RequestedPath $ExePath
$releaseRootFull = Resolve-FullPath -PathValue $ReleaseRoot
$lastReleasePathFull = Resolve-FullPath -PathValue $LastReleasePathFile

New-Item -ItemType Directory -Force -Path $releaseRootFull | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$releaseDir = Join-Path $releaseRootFull "radiotedu-broadcast-room-portable-$stamp"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

$portableExePath = Join-Path $releaseDir $BackendExeName
Copy-Item -Path $exeFull -Destination $portableExePath -Force

$portableExeFull = [System.IO.Path]::GetFullPath($portableExePath)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($lastReleasePathFull, $portableExeFull, $utf8NoBom)

Write-Output "Portable release folder: $releaseDir"
Write-Output "Portable executable: $portableExeFull"
Write-Output "Recorded latest portable release path: $lastReleasePathFull"

if ($OpenFolder) {
    Start-Process explorer.exe $releaseDir
}
