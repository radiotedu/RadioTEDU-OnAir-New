param(
    [string]$Version = "",
    [string]$Configuration = "Release",
    [string]$RuntimeIdentifier = "win-x64",
    [string]$InnoSetupCompiler = $env:INNO_SETUP_COMPILER,
    [string]$SignTool = $env:RADIOTEDU_SIGNTOOL,
    [string]$SigningCertificateThumbprint = $env:RADIOTEDU_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$RequireSignature,
    [switch]$SkipBackendBuild
)

$ErrorActionPreference = "Stop"
$BackendExeName = "RadioTEDU-OnAir-Backend.exe"
$SupervisorExeName = "RadioTEDU-OnAir-Supervisor.exe"
$ShellExeName = "RadioTEDU-OnAir.exe"
$SetupScriptName = "RadioTEDUBroadcastRoomSetup.iss"
$SetupBaseNamePrefix = "RadioTEDU-OnAir-Setup"
$ProductDisplayName = "RadioTEDU OnAir"
$BackendDistDirectoryName = "backend"
$BackendPublishDirectoryName = "radiotedu-backend-publish"
$DesktopDistDirectoryName = "desktop"
$ReleaseDirectoryName = "setup"

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

function Resolve-PythonInstallCommand {
    $pythonCommand = "python"
    $pythonPrefixArgs = @()

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
        }
    }

    if (-not (Test-PythonTarget -Command $pythonCommand -PrefixArgs $pythonPrefixArgs)) {
        throw "Could not run Python command for installer packaging: $pythonCommand $($pythonPrefixArgs -join ' ')"
    }

    return [pscustomobject]@{
        Command = $pythonCommand
        PrefixArgs = $pythonPrefixArgs
    }
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$versionFile = Join-Path $root "..\VERSION"
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw "Product VERSION file is missing: $versionFile"
}
$sourceVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if ($sourceVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Product VERSION file is invalid."
}
if ($Version -and $Version -ne $sourceVersion) {
    throw "Requested version '$Version' does not match product VERSION '$sourceVersion'."
}
$Version = $sourceVersion

function Initialize-LocalBuildEnvironment {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $root ".."))
    $tempRoot = Join-Path $repoRoot ".tmp\build-temp"
    $pipCache = Join-Path $repoRoot ".tmp\pip-cache"
    New-Item -ItemType Directory -Force -Path $tempRoot, $pipCache | Out-Null
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    $env:PIP_CACHE_DIR = $pipCache
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
}

function Resolve-InnoSetupCommand {
    param([string]$ExplicitCompiler = "")

    if (-not [string]::IsNullOrWhiteSpace($ExplicitCompiler)) {
        if (-not (Test-Path $ExplicitCompiler -PathType Leaf)) {
            throw "Explicit Inno Setup compiler was not found: $ExplicitCompiler"
        }
        return [System.IO.Path]::GetFullPath($ExplicitCompiler)
    }

    $knownPath = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Path,
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

    if ($knownPath) {
        return [System.IO.Path]::GetFullPath($knownPath)
    }

    throw "ISCC.exe was not found. Install Inno Setup 6, pass -InnoSetupCompiler, or set INNO_SETUP_COMPILER."
}

Initialize-LocalBuildEnvironment

function Ensure-BrandAssets {
    $brandScript = Join-Path $root "generate_brand_assets.ps1"
    if (-not (Test-Path -LiteralPath $brandScript -PathType Leaf)) {
        throw "RadioTEDU brand asset generator not found: $brandScript"
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $brandScript | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "RadioTEDU installer brand asset generation failed."
    }
}

function Ensure-DesktopBundle {
    $bundleScript = Join-Path $root "..\build_desktop_bundle.ps1"
    if (-not (Test-Path $bundleScript)) {
        throw "Desktop bundle script not found: $bundleScript"
    }

    Write-Host "Building desktop bundle via $bundleScript"
    $bundleArgs = @(
        "-ExecutionPolicy"
        "Bypass"
        "-File"
        $bundleScript
        "-Configuration"
        $Configuration
        "-RuntimeIdentifier"
        $RuntimeIdentifier
    )
    if ([bool]$SkipBackendBuild) {
        $bundleArgs += "-SkipBackendBuild"
    }

    & powershell @bundleArgs | Out-Host

    if ($LASTEXITCODE -ne 0) {
        throw "Desktop bundle build failed."
    }

    $bundleRoot = Join-Path $root "..\dist\$DesktopDistDirectoryName"
    $expectedArtifacts = @(
        (Join-Path $bundleRoot "shell\$ShellExeName"),
        (Join-Path $bundleRoot "supervisor\$SupervisorExeName")
    )

    foreach ($artifact in $expectedArtifacts) {
        if (-not (Test-Path $artifact)) {
            throw "Expected desktop bundle artifact was not produced: $artifact"
        }
    }
}

function Resolve-BackendArtifactPath {
    $candidatePaths = @(
        (Join-Path $root "..\dist\$BackendDistDirectoryName\$BackendExeName"),
        (Join-Path $root "..\build\$BackendPublishDirectoryName\$([System.IO.Path]::GetFileNameWithoutExtension($BackendExeName))\$BackendExeName")
    )

    foreach ($candidate in $candidatePaths) {
        if (Test-Path $candidate) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }

    throw "Backend artifact was not produced in an expected location."
}

function Ensure-PythonRequirements {
    $requirementsPath = Join-Path $root "..\requirements.lock"
    if (-not (Test-Path $requirementsPath)) {
        throw "Locked Python requirements file not found: $requirementsPath"
    }

    $pythonInstall = Resolve-PythonInstallCommand
    $displayName = $pythonInstall.Command
    if (@($pythonInstall.PrefixArgs).Count -gt 0) {
        $displayName = "$displayName $($pythonInstall.PrefixArgs -join ' ')"
    }

    Write-Host "Installing locked Python requirements via $displayName -m pip install -r $requirementsPath"
    & $pythonInstall.Command @($pythonInstall.PrefixArgs) -m pip install --only-binary=:all: -r $requirementsPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Python requirements installation failed."
    }
}

function Reset-InstallerOutput {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseDir
    )

    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

    Get-ChildItem -Path $ReleaseDir -Filter "$SetupBaseNamePrefix-*.exe" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Get-ChildItem -Path $ReleaseDir -Filter "$SetupBaseNamePrefix-*.sha256" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Get-ChildItem -Path $ReleaseDir -Filter "$SetupBaseNamePrefix-*.provenance.json" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    $markerPath = Join-Path $ReleaseDir "last_setup_path.txt"
    if (Test-Path $markerPath) {
        Remove-Item $markerPath -Force -ErrorAction SilentlyContinue
    }
}

Ensure-BrandAssets
Ensure-DesktopBundle

$releaseValidator = Join-Path $root "..\scripts\validate_radiotedu_release.py"
$validationPython = Resolve-PythonInstallCommand
$releaseValidationArgs = @(
    $releaseValidator
    "--backend"
    (Join-Path $root "..\dist\$BackendDistDirectoryName")
    "--shell"
    (Join-Path $root "..\dist\$DesktopDistDirectoryName\shell")
    "--supervisor"
    (Join-Path $root "..\dist\$DesktopDistDirectoryName\supervisor")
)
& $validationPython.Command @($validationPython.PrefixArgs) @releaseValidationArgs | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Focused release-scope validation failed."
}

$backendArtifactPath = Resolve-BackendArtifactPath
$backendMarkerPath = Join-Path $root "..\last_build_path.txt"
Set-Content -Path $backendMarkerPath -Value $backendArtifactPath -Encoding UTF8
Write-Output $backendArtifactPath
Write-Output "Recorded latest backend path: $backendMarkerPath"

$iscc = Resolve-InnoSetupCommand -ExplicitCompiler $InnoSetupCompiler
$setupScript = Join-Path $root $SetupScriptName
if (-not (Test-Path $setupScript)) {
    throw "Installer script not found: $setupScript"
}

$releaseDir = Join-Path $root "..\release\$ReleaseDirectoryName"
Reset-InstallerOutput -ReleaseDir $releaseDir
$markerPath = Join-Path $releaseDir "last_setup_path.txt"

$setupBaseName = "$SetupBaseNamePrefix-$Version"
Write-Host "Building installer $setupBaseName.exe into $releaseDir"
$isccArguments = @(
    "/DAppVersion=$Version"
    "/O$releaseDir"
    "/F$setupBaseName"
    $setupScript
)

$process = Start-Process -FilePath $iscc -ArgumentList $isccArguments -Wait -PassThru
if ($process.ExitCode -ne 0) {
    throw "Inno Setup build failed."
}

$setupPath = Join-Path $releaseDir "$setupBaseName.exe"
if (-not (Test-Path $setupPath)) {
    throw "Expected installer was not produced: $setupPath"
}

$setupPath = [System.IO.Path]::GetFullPath($setupPath)
$signed = $false
if ($SignTool -or $SigningCertificateThumbprint) {
    if (-not $SignTool -or -not (Test-Path -LiteralPath $SignTool -PathType Leaf)) {
        throw "A valid -SignTool path is required when installer signing is requested."
    }
    if ($SigningCertificateThumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
        throw "-SigningCertificateThumbprint must be a 40-character SHA-1 certificate thumbprint."
    }
    & $SignTool sign /sha1 $SigningCertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $setupPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode signing failed."
    }
    & $SignTool verify /pa /all $setupPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode verification failed."
    }
    $signed = $true
}
elseif ($RequireSignature) {
    throw "A signed installer is required, but signing configuration was not supplied."
}

$checksumPath = Join-Path $releaseDir "$setupBaseName.sha256"
$checksum = (Get-FileHash -Path $setupPath -Algorithm SHA256).Hash
Set-Content -Path $checksumPath -Value "$checksum  $setupBaseName.exe" -Encoding ASCII
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $root ".."))
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "Git is required to generate exact release provenance."
}
$sourceGitCommit = (& $git.Source -C $repositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceGitCommit -notmatch "^[0-9a-fA-F]{40}$") {
    throw "Could not resolve the exact source commit for release provenance."
}
$trackedStatus = @(& $git.Source -C $repositoryRoot status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) {
    throw "Could not verify the tracked source tree for release provenance."
}
$sourceGitTrackedTreeDirty = $trackedStatus.Count -gt 0
if ($sourceGitTrackedTreeDirty) {
    throw "Release provenance requires a clean tracked source tree."
}

$backendProvenancePath = Join-Path (Split-Path -Parent $backendArtifactPath) "build-provenance.json"
if (-not (Test-Path -LiteralPath $backendProvenancePath -PathType Leaf)) {
    throw "Backend provenance is missing: $backendProvenancePath"
}
$backendProvenance = Get-Content -LiteralPath $backendProvenancePath -Raw | ConvertFrom-Json
$shellArtifactPath = Join-Path $repositoryRoot "dist\desktop\shell\$ShellExeName"
$supervisorArtifactPath = Join-Path $repositoryRoot "dist\desktop\supervisor\$SupervisorExeName"
$watchdogScriptPath = Join-Path $repositoryRoot "tools\RadioTEDU-AudioWatchdog.ps1"
$watchdogInstallerPath = Join-Path $root "InstallAudioWatchdog.ps1"
$supervisorInstallerPath = Join-Path $root "InstallSupervisorService.ps1"
foreach ($releaseInput in @(
    $shellArtifactPath,
    $supervisorArtifactPath,
    $watchdogScriptPath,
    $watchdogInstallerPath,
    $supervisorInstallerPath
)) {
    if (-not (Test-Path -LiteralPath $releaseInput -PathType Leaf)) {
        throw "Release provenance input is missing: $releaseInput"
    }
}

$provenancePath = Join-Path $releaseDir "$setupBaseName.provenance.json"
[ordered]@{
    schema_version = 2
    product = $ProductDisplayName
    product_version = $Version
    installer = $setupBaseName + ".exe"
    installer_sha256 = $checksum
    authenticode_signed = $signed
    source_git_commit = $sourceGitCommit
    source_git_tracked_tree_dirty = $sourceGitTrackedTreeDirty
    backend_git_commit = [string]$backendProvenance.git_commit
    backend_source_sha256 = [string]$backendProvenance.source_sha256
    backend_executable_sha256 = (Get-FileHash -LiteralPath $backendArtifactPath -Algorithm SHA256).Hash
    desktop_shell_sha256 = (Get-FileHash -LiteralPath $shellArtifactPath -Algorithm SHA256).Hash
    desktop_supervisor_sha256 = (Get-FileHash -LiteralPath $supervisorArtifactPath -Algorithm SHA256).Hash
    installer_definition_sha256 = (Get-FileHash -LiteralPath $setupScript -Algorithm SHA256).Hash
    supervisor_installer_sha256 = (Get-FileHash -LiteralPath $supervisorInstallerPath -Algorithm SHA256).Hash
    audio_watchdog_installer_sha256 = (Get-FileHash -LiteralPath $watchdogInstallerPath -Algorithm SHA256).Hash
    audio_watchdog_script_sha256 = (Get-FileHash -LiteralPath $watchdogScriptPath -Algorithm SHA256).Hash
    generated_utc = [DateTimeOffset]::UtcNow.ToString("O")
} | ConvertTo-Json | Set-Content -LiteralPath $provenancePath -Encoding UTF8
Set-Content -Path $markerPath -Value $setupPath -Encoding UTF8
Write-Output $setupPath
Write-Output "SHA-256 checksum: $checksumPath"
Write-Output "Release provenance: $provenancePath"
Write-Output "Recorded latest installer path: $markerPath"
