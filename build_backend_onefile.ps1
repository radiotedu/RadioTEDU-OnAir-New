param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$BackendExeName = "RadioTEDU-OnAir-Backend"
$BackendEntrypoint = "run_cleanroom.py"
$SupervisorExeName = "RadioTEDU-OnAir-Supervisor.exe"
$RuntimePort = 8100
$BackendDistRelative = ".\dist\backend"
$BackendBuildSlug = "radiotedu"
$StaticData = ".\build\radiotedu-static-stage;app\static"
$QwenTtsCliData = ".\app\services\qwen_tts_cli.py;app\services"
$QwenTtsServerData = ".\app\services\qwen_tts_server.py;app\services"
$OmniVoiceCliData = ".\app\services\omnivoice_cli.py;app\services"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$configuredScratchRoot = $env:RADIOTEDU_BUILD_SCRATCH_ROOT
$defaultScratchName = "RadioTEDU-OnAir-Build"
$buildScratchRoot = if ([string]::IsNullOrWhiteSpace($configuredScratchRoot)) {
    Join-Path ([System.IO.Path]::GetTempPath()) $defaultScratchName
}
else {
    [System.IO.Path]::GetFullPath($configuredScratchRoot)
}

function Initialize-LocalBuildEnvironment {
    $tempRoot = Join-Path $buildScratchRoot "build-temp"
    $pipCache = Join-Path $buildScratchRoot "pip-cache"
    New-Item -ItemType Directory -Force -Path $tempRoot, $pipCache | Out-Null
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    $env:PIP_CACHE_DIR = $pipCache
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    }
    catch {
        Write-Warning "Could not force TLS 1.2 for build downloads."
    }
}

function Test-PythonTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$PrefixArgs = @()
    )

    try {
        & $Command @PrefixArgs -c "import sys; print(sys.version_info[0])" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Ensure-PythonPackageInstalled {
    param(
        [Parameter(Mandatory = $true)][string]$ImportName,
        [string]$InstallSpec = ""
    )

    $spec = if ($InstallSpec) { $InstallSpec } else { $ImportName }
    $probeCode = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ImportName') else 1)"
    $installed = $false
    try {
        & $pythonCommand @pythonPrefixArgs -c $probeCode *> $null
        $installed = ($LASTEXITCODE -eq 0)
    }
    catch {
        $installed = $false
    }

    if ($installed) {
        Write-Output "Using Python package $ImportName"
        return
    }

    Write-Output "Installing missing Python package: $spec"
    & $pythonCommand @pythonPrefixArgs -m pip install $spec | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install package: $spec"
    }
}

Initialize-LocalBuildEnvironment

$pythonCommand = $Python
$pythonPrefixArgs = @()

if ($Python -eq "python") {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $installed = @()
        try {
            $installed = @(& py -0p 2>$null)
        }
        catch {
            $installed = @()
        }
        $has312 = $false
        foreach ($line in $installed) {
            if ($line -match "V:3\.12") {
                $has312 = $true
                break
            }
        }
        if ($has312) {
            $pythonCommand = "py"
            $pythonPrefixArgs = @("-3.12")
            Write-Output "Using Python launcher target: py -3.12"
        }
    }
}

if (-not (Test-PythonTarget -Command $pythonCommand -PrefixArgs $pythonPrefixArgs)) {
    throw "Could not run Python command: $pythonCommand $($pythonPrefixArgs -join ' ')"
}

$versionText = (& $pythonCommand @pythonPrefixArgs -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not read Python version from: $pythonCommand $($pythonPrefixArgs -join ' ')"
}

if ($versionText -match '^\d+\.\d+$') {
    $parts = $versionText.Split('.')
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -ne 3 -or $minor -ne 12) {
        throw "Packaged backend builds require Python 3.12; found $versionText."
    }
}
else {
    Write-Warning "Could not parse Python version output: '$versionText'"
}

function Remove-PathWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Attempts = 8,
        [int]$DelayMs = 400
    )

    if (-not (Test-Path $Path)) {
        return $true
    }

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Remove-Item $Path -Recurse -Force -ErrorAction Stop
            return $true
        }
        catch {
            if ($i -eq $Attempts) {
                return $false
            }
            Start-Sleep -Milliseconds $DelayMs
        }
    }

    return $false
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [int]$Attempts = 8,
        [int]$DelayMs = 400
    )

    if (-not (Test-Path $Source -PathType Container)) {
        throw "Source directory not found: $Source"
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        $copied = $false
        for ($i = 1; $i -le $Attempts; $i++) {
            try {
                Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force -ErrorAction Stop
                $copied = $true
                break
            }
            catch {
                if ($i -eq $Attempts) {
                    throw
                }
                Start-Sleep -Milliseconds $DelayMs
            }
        }

        if (-not $copied) {
            throw "Could not copy build artifact into package: $($_.FullName)"
        }
    }
}

function Get-BackendSourceManifest {
    $appRoot = Join-Path $root "app"
    $files = @(
        Get-ChildItem -LiteralPath $appRoot -Recurse -File |
            Where-Object {
                if ($_.Extension -in @(".pyc", ".pyo") -or $_.FullName -match '[\\/]__pycache__[\\/]') {
                    return $false
                }
                $appRelative = $_.FullName.Substring($appRoot.Length).TrimStart("\", "/").Replace("\", "/")
                if ($appRelative.StartsWith("static/rtai/", [System.StringComparison]::OrdinalIgnoreCase) -or
                    $appRelative.StartsWith("rtai/", [System.StringComparison]::OrdinalIgnoreCase) -or
                    $appRelative.StartsWith("api/rtai_", [System.StringComparison]::OrdinalIgnoreCase)) {
                    # This checkout is the standalone RadioTEDU product. Keep
                    # the separately-owned rtAI product out of its artifact.
                    return $false
                }
                return $true
            }
    ) + @(
        (Get-Item -LiteralPath (Join-Path $root $BackendEntrypoint))
        (Get-Item -LiteralPath (Join-Path $root "requirements.lock"))
        (Get-Item -LiteralPath (Join-Path $root "VERSION"))
        (Get-Item -LiteralPath (Join-Path $root "build_backend_onefile.ps1"))
    )
    foreach ($file in @($files | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($root.Length).TrimStart("\", "/").Replace("\", "/")
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
        [PSCustomObject]@{
            RelativePath = $relative
            Sha256 = $hash
        }
    }
}

function Get-BackendSourceFingerprint {
    param([Parameter(Mandatory = $true)][object[]]$Manifest)

    $material = New-Object System.Text.StringBuilder
    foreach ($entry in @($Manifest | Sort-Object RelativePath)) {
        [void]$material.Append($entry.RelativePath).Append(":").Append($entry.Sha256).Append("`n")
    }
    $provider = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($material.ToString())
        return ([System.BitConverter]::ToString($provider.ComputeHash($bytes))).Replace("-", "")
    }
    finally {
        $provider.Dispose()
    }
}

function Add-LocalPythonPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $parts = @()
    if ($env:PYTHONPATH) {
        $parts = @($env:PYTHONPATH -split [System.IO.Path]::PathSeparator)
    }
    if ($parts -notcontains $resolved) {
        $env:PYTHONPATH = (@($resolved) + $parts) -join [System.IO.Path]::PathSeparator
    }
}

function Get-PyInstallerVersion {
    try {
        $version = (& $pythonCommand @pythonPrefixArgs -m PyInstaller --version).Trim()
        if ($LASTEXITCODE -eq 0 -and $version) {
            return $version
        }
    }
    catch {
    }

    try {
        $version = (& $pythonCommand @pythonPrefixArgs -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
        if ($LASTEXITCODE -ne 0) {
            return ""
        }
        return $version
    }
    catch {
        return ""
    }
}

function Resolve-PyPiWheelUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Package,
        [string]$Version = "",
        [string[]]$PreferredPatterns = @("*py3-none-any.whl", "*py2.py3-none-any.whl", "*py3-none-win_amd64.whl")
    )

    $metadataUrl = if ($Version) {
        "https://pypi.org/pypi/$Package/$Version/json"
    }
    else {
        "https://pypi.org/pypi/$Package/json"
    }

    $metadata = Invoke-RestMethodWithRetry -Uri $metadataUrl
    $wheels = @($metadata.urls | Where-Object { $_.packagetype -eq "bdist_wheel" })
    foreach ($pattern in $PreferredPatterns) {
        $match = $wheels | Where-Object { $_.filename -like $pattern } | Select-Object -First 1
        if ($match) {
            return [pscustomobject]@{
                FileName = $match.filename
                Url = $match.url
            }
        }
    }

    throw "No compatible wheel found for $Package $Version."
}

function Invoke-RestMethodWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [int]$Attempts = 4,
        [int]$DelaySeconds = 3
    )

    $hashProvider = [System.Security.Cryptography.SHA256]::Create()
    $hashBytes = $hashProvider.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Uri))
    $hash = ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    $jsonDir = Join-Path $root ".tmp\pypi-json"
    New-Item -ItemType Directory -Force -Path $jsonDir | Out-Null
    $jsonPath = Join-Path $jsonDir "$hash.json"

    Invoke-WebRequestWithRetry -Uri $Uri -OutFile $jsonPath -Attempts $Attempts -DelaySeconds $DelaySeconds
    return Get-Content -Path $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Invoke-WebRequestWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$OutFile,
        [int]$Attempts = 4,
        [int]$DelaySeconds = 3
    )

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $parentDir = Split-Path -Parent $OutFile
            if ($parentDir) {
                New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
            }
            $downloadCode = @"
import pathlib
import shutil
import sys
import urllib.request

url = sys.argv[1]
destination = pathlib.Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
with urllib.request.urlopen(url, timeout=90) as response:
    with destination.open('wb') as output:
        shutil.copyfileobj(response, output)
"@
            & $pythonCommand @pythonPrefixArgs -c $downloadCode $Uri $OutFile
            if ($LASTEXITCODE -ne 0) {
                throw "Python download failed with exit code $LASTEXITCODE."
            }
            return
        }
        catch {
            if ($i -eq $Attempts) {
                throw
            }
            Write-Warning "Download failed ($i/$Attempts): $Uri. Retrying in $DelaySeconds seconds."
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Install-PyPiWheelToTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Package,
        [Parameter(Mandatory = $true)][string]$TargetDir,
        [string]$Version = "",
        [string[]]$PreferredPatterns = @("*py3-none-any.whl", "*py2.py3-none-any.whl", "*py3-none-win_amd64.whl")
    )

    $wheel = Resolve-PyPiWheelUrl -Package $Package -Version $Version -PreferredPatterns $PreferredPatterns
    $wheelDir = Join-Path $root ".tmp\wheels"
    New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
    $wheelPath = Join-Path $wheelDir $wheel.FileName

    if (-not (Test-Path $wheelPath -PathType Leaf)) {
        Write-Output "Downloading Python wheel: $($wheel.FileName)"
        Invoke-WebRequestWithRetry -Uri $wheel.Url -OutFile $wheelPath
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($wheelPath, $TargetDir)
}

function Ensure-LocalPyInstaller {
    param([Parameter(Mandatory = $true)][string]$Version)

    $localSite = Join-Path $root "build\python-packages"
    Add-LocalPythonPath -Path $localSite

    if (
        (Test-Path (Join-Path $localSite "PyInstaller\__init__.py") -PathType Leaf) `
        -and (Get-PyInstallerVersion) -eq $Version
    ) {
        Write-Output "Using repo-local PyInstaller $Version"
        return
    }

    if (-not (Remove-PathWithRetry -Path $localSite)) {
        throw "Could not clean repo-local Python package folder: $localSite"
    }
    New-Item -ItemType Directory -Force -Path $localSite | Out-Null

    Install-PyPiWheelToTarget `
        -Package "pyinstaller" `
        -Version $Version `
        -TargetDir $localSite `
        -PreferredPatterns @("*py3-none-win_amd64.whl")
    Install-PyPiWheelToTarget -Package "altgraph" -Version "0.17.5" -TargetDir $localSite
    Install-PyPiWheelToTarget -Package "pefile" -Version "2024.8.26" -TargetDir $localSite
    Install-PyPiWheelToTarget -Package "pywin32-ctypes" -Version "0.2.3" -TargetDir $localSite
    Install-PyPiWheelToTarget -Package "pyinstaller-hooks-contrib" -Version "2026.6" -TargetDir $localSite

    Add-LocalPythonPath -Path $localSite
    $installedVersion = Get-PyInstallerVersion
    if ($installedVersion -ne $Version) {
        throw "Repo-local PyInstaller validation failed. Expected $Version, got '$installedVersion'."
    }

    Write-Output "Installed repo-local PyInstaller $Version into $localSite"
}

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue)
$ffplay = (Get-Command ffplay -ErrorAction SilentlyContinue)
$ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue)

if (-not $ffmpeg) {
    throw "ffmpeg not found in PATH. Install or add ffmpeg.exe to PATH before build."
}
if (-not $ffplay) {
    throw "ffplay not found in PATH. Install or add ffplay.exe to PATH before build."
}
if (-not $ffprobe) {
    throw "ffprobe not found in PATH. Install or add ffprobe.exe to PATH before build."
}

$lockedRequirements = Join-Path $root "requirements.lock"
if (-not (Test-Path $lockedRequirements -PathType Leaf)) {
    throw "Locked Python requirements file not found: $lockedRequirements"
}
$sourceManifestBefore = @(Get-BackendSourceManifest)
$sourceFingerprintBefore = Get-BackendSourceFingerprint -Manifest $sourceManifestBefore
$buildVenv = Join-Path $buildScratchRoot "backend-build-venv"
if (-not (Remove-PathWithRetry -Path $buildVenv)) {
    $scratchStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $buildVenv = Join-Path $buildScratchRoot "backend-build-venv-$scratchStamp"
    Write-Warning "Prior isolated environment remains scanner-locked; using fresh fallback: $buildVenv"
    if (-not (Remove-PathWithRetry -Path $buildVenv)) {
        throw "Could not prepare fallback isolated Python build environment: $buildVenv"
    }
}
$bootstrapPythonCommand = $pythonCommand
$bootstrapPythonPrefixArgs = @($pythonPrefixArgs)
Write-Output "Creating isolated Python 3.12 build environment: $buildVenv"
& $bootstrapPythonCommand @bootstrapPythonPrefixArgs -m venv $buildVenv
if ($LASTEXITCODE -ne 0) {
    throw "Could not create isolated Python build environment."
}
$venvPythonCandidates = @(
    (Join-Path $buildVenv "Scripts\python.exe"),
    (Join-Path $buildVenv "Scripts\python.cmd")
)
$venvPython = $venvPythonCandidates | Where-Object {
    Test-Path $_ -PathType Leaf
} | Select-Object -First 1
if (-not $venvPython) {
    throw "Isolated Python executable was not created under $buildVenv."
}
$pythonCommand = $venvPython
$pythonPrefixArgs = @()
# A caller-provided PYTHONPATH would defeat virtual-environment isolation and
# let globally installed optional/AI packages affect PyInstaller analysis.
$env:PYTHONPATH = ""

$requiredPyInstaller = "6.19.0"
Write-Output "Installing deterministic Python build and runtime lock into $buildVenv"
$pipInstallArgs = @(
    "--quiet",
    "--disable-pip-version-check",
    "--only-binary=:all:",
    "--no-compile",
    "--force-reinstall",
    "--upgrade",
    "-r", $lockedRequirements,
    "pyinstaller==$requiredPyInstaller",
    "pyinstaller-hooks-contrib==2026.6",
    "altgraph==0.17.5",
    "pefile==2024.8.26",
    "pywin32-ctypes==0.2.3",
    "setuptools==80.9.0"
)
$pipInstallExitCode = -1
for ($attempt = 1; $attempt -le 6; $attempt++) {
    $priorPipErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $pythonCommand @pythonPrefixArgs -m pip install @pipInstallArgs 2>&1 | Out-Host
    $pipInstallExitCode = $LASTEXITCODE
    $ErrorActionPreference = $priorPipErrorActionPreference
    if ($pipInstallExitCode -eq 0) {
        break
    }
    if ($attempt -lt 6) {
        # Windows Defender/indexers can briefly retain a newly extracted wheel
        # member. Retrying the same locked transaction is deterministic and
        # lets pip repair the partially installed environment.
        Write-Warning "Locked Python dependency install attempt $attempt failed; retrying after transient file-handle delay."
        Start-Sleep -Seconds (2 * $attempt)
    }
}
if ($pipInstallExitCode -ne 0) {
    throw "Locked Python build dependency installation failed."
}
& $pythonCommand @pythonPrefixArgs -m pip check | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Locked Python dependency graph is inconsistent."
}
$buildImportProbe = @"
import importlib
modules = (
    'packaging', 'PyInstaller', 'fastapi', 'starlette', 'uvicorn',
    'pydantic', 'cryptography', 'jose', 'passlib', 'bcrypt',
    'requests', 'httpx', 'websockets', 'multipart'
)
for name in modules:
    importlib.import_module(name)
print('Locked build imports validated:', len(modules))
"@
& $pythonCommand @pythonPrefixArgs -c $buildImportProbe | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Locked Python packages passed metadata checks but failed import validation."
}
if ((Get-PyInstallerVersion) -ne $requiredPyInstaller) {
    throw "Isolated PyInstaller validation failed after locked install."
}

# Stop only disposable build-output processes. Never terminate an installed or
# commissioned backend merely because a developer started a packaging build.
$allowedProcessRoots = @(
    [System.IO.Path]::GetFullPath((Join-Path $root "build")),
    [System.IO.Path]::GetFullPath((Join-Path $root "dist"))
)
$running = @(Get-Process -Name $BackendExeName -ErrorAction SilentlyContinue) | Where-Object {
    $processPath = $_.Path
    if (-not $processPath) { return $false }
    $resolvedProcessPath = [System.IO.Path]::GetFullPath($processPath)
    return [bool]($allowedProcessRoots | Where-Object {
        $resolvedProcessPath.StartsWith($_ + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
    })
}
if ($running) {
    $running | ForEach-Object {
        Write-Output "Stopping running process PID=$($_.Id) ($($_.Path))"
        Stop-Process -Id $_.Id -Force
    }
    Start-Sleep -Milliseconds 600
}

# Choose and clean the release destination only after disposable repository
# processes have exited. FFmpeg and station-worker children inherit files from
# the same bundle and can otherwise force a timestamp fallback even though the
# canonical directory becomes writable milliseconds later.
$distDir = $BackendDistRelative
if (-not (Remove-PathWithRetry -Path $distDir)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $distDir = "$BackendDistRelative-$stamp"
    Write-Warning "Could not clean $BackendDistRelative (locked). Building into $distDir instead."
    if (-not (Remove-PathWithRetry -Path $distDir)) {
        throw "Could not clean fallback output folder: $distDir"
    }
}

$pyInstallerDistRoot = ".\build\$BackendBuildSlug-backend-publish"
$pyInstallerWorkRoot = ".\build\$BackendBuildSlug-pyinstaller-work"
$staticStageRoot = ".\build\radiotedu-static-stage"

$buildPaths = @($pyInstallerDistRoot, $pyInstallerWorkRoot, $staticStageRoot)
foreach ($buildPath in $buildPaths) {
    if (-not (Remove-PathWithRetry -Path $buildPath)) {
        throw "Could not clean build path (locked by another process): $buildPath"
    }
}

$sourceStaticRoot = Join-Path $root "app\static"
$resolvedStaticStageRoot = [System.IO.Path]::GetFullPath((Join-Path $root $staticStageRoot))
New-Item -ItemType Directory -Force -Path $resolvedStaticStageRoot | Out-Null
if (Test-Path -LiteralPath $sourceStaticRoot -PathType Container) {
    Get-ChildItem -LiteralPath $sourceStaticRoot -Force |
        Where-Object { $_.Name -ne "rtai" } |
        ForEach-Object {
            if ($_.PSIsContainer) {
                Copy-DirectoryContents `
                    -Source $_.FullName `
                    -Destination (Join-Path $resolvedStaticStageRoot $_.Name)
            }
            else {
                Copy-Item -LiteralPath $_.FullName -Destination $resolvedStaticStageRoot -Force
            }
        }
}
else {
    Write-Warning "Legacy static source root is absent; continuing so the staged-output gate can report the actual packaging failure."
}

$priorErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $pythonCommand @pythonPrefixArgs -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --console `
    --distpath $pyInstallerDistRoot `
    --workpath $pyInstallerWorkRoot `
    --name $BackendExeName `
    --add-binary "$($ffmpeg.Source);." `
    --add-binary "$($ffprobe.Source);." `
    --add-data $StaticData `
    --add-data $QwenTtsCliData `
    --add-data $QwenTtsServerData `
    --add-data $OmniVoiceCliData `
    --add-data ".\VERSION;." `
    --collect-all fastapi `
    --collect-all starlette `
    --collect-all uvicorn `
    --collect-all pydantic `
    --collect-all tzdata `
    --hidden-import "passlib.handlers.bcrypt" `
    --exclude-module "torch" `
    --exclude-module "torchvision" `
    --exclude-module "torchaudio" `
    --exclude-module "transformers" `
    --exclude-module "matplotlib" `
    --exclude-module "scipy" `
    --exclude-module "pytest" `
    --exclude-module "librosa" `
    --exclude-module "numba" `
    --exclude-module "llvmlite" `
    --exclude-module "pandas" `
    --exclude-module "onnxruntime" `
    --exclude-module "tensorflow" `
    ".\\$BackendEntrypoint" 2>&1 | Out-Host
$pyInstallerExitCode = $LASTEXITCODE
$ErrorActionPreference = $priorErrorActionPreference

if ($pyInstallerExitCode -ne 0) {
    throw "PyInstaller failed with exit code $pyInstallerExitCode."
}

$sourceManifestAfter = @(Get-BackendSourceManifest)
$sourceFingerprintAfter = Get-BackendSourceFingerprint -Manifest $sourceManifestAfter
if ($sourceFingerprintAfter -ne $sourceFingerprintBefore) {
    $beforeByPath = @{}
    $afterByPath = @{}
    $sourceManifestBefore | ForEach-Object { $beforeByPath[$_.RelativePath] = $_.Sha256 }
    $sourceManifestAfter | ForEach-Object { $afterByPath[$_.RelativePath] = $_.Sha256 }
    $changedPaths = @(
        @($beforeByPath.Keys) + @($afterByPath.Keys) |
            Select-Object -Unique |
            Where-Object { $beforeByPath[$_] -ne $afterByPath[$_] } |
            Sort-Object
    )
    if ($changedPaths.Count -gt 0) {
        Write-Error ("Backend source paths changed during packaging: " + ($changedPaths -join ", "))
    }
    throw "Backend source changed during packaging. Discard this artifact and rebuild from a stable tree."
}

$stagedOut = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $root $pyInstallerDistRoot) $BackendExeName))
if (-not (Test-Path $stagedOut -PathType Container) -or
    -not (Test-Path (Join-Path $stagedOut "$BackendExeName.exe") -PathType Leaf)) {
    throw "Expected staged backend bundle was not produced at $stagedOut."
}

# PyInstaller places runtime binaries under _internal for an onedir bundle. Keep
# explicit managed-tool copies as well: the installer/bootstrap contract uses
# tools\bin as its stable, versioned source when publishing FFmpeg dependencies.
$stagedToolsBin = Join-Path $stagedOut "tools\bin"
New-Item -ItemType Directory -Force -Path $stagedToolsBin | Out-Null
Copy-Item -LiteralPath $ffmpeg.Source -Destination (Join-Path $stagedToolsBin "ffmpeg.exe") -Force
Copy-Item -LiteralPath $ffplay.Source -Destination (Join-Path $stagedToolsBin "ffplay.exe") -Force
Copy-Item -LiteralPath $ffprobe.Source -Destination (Join-Path $stagedToolsBin "ffprobe.exe") -Force

$pythonBuildVersion = (& $pythonCommand @pythonPrefixArgs -c "import platform; print(platform.python_version())").Trim()
$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    throw "Git is required to record release provenance."
}
$gitCommit = (& $gitCommand.Path -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Could not resolve an exact Git commit for release provenance."
}
$gitTrackedChanges = @(& $gitCommand.Path -C $root status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) {
    throw "Could not determine tracked-tree state for release provenance."
}
$provenance = [ordered]@{
    schema_version = 1
    product_version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -Raw).Trim()
    git_commit = $gitCommit
    git_tracked_tree_dirty = [bool]($gitTrackedChanges.Count -gt 0)
    source_sha256 = $sourceFingerprintAfter
    dependency_lock_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $lockedRequirements).Hash
    python_version = $pythonBuildVersion
    pyinstaller_version = $requiredPyInstaller
    ffmpeg_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffmpeg.Source).Hash
    ffplay_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffplay.Source).Hash
    ffprobe_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ffprobe.Source).Hash
}
$provenance | ConvertTo-Json | Set-Content `
    -LiteralPath (Join-Path $stagedOut "build-provenance.json") `
    -Encoding UTF8

$distOut = [System.IO.Path]::GetFullPath((Join-Path $root $distDir))
Copy-DirectoryContents -Source $stagedOut -Destination $distOut

$builtExe = [System.IO.Path]::GetFullPath((Join-Path $distOut "$BackendExeName.exe"))
if (-not (Test-Path $builtExe)) {
    throw "Expected backend executable was not produced at $builtExe."
}

$lastPathFile = Join-Path $root "last_build_path.txt"
Set-Content -Path $lastPathFile -Value $builtExe -Encoding UTF8
Write-Output "Built backend package: $distOut"
Write-Output "Backend executable: $builtExe"
Write-Output "Recorded latest build path: $lastPathFile"
Write-Output "Runtime note: the packaged backend is launched hidden by $SupervisorExeName and binds to 127.0.0.1:$RuntimePort by default."
