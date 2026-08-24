import subprocess
import sys
from pathlib import Path


def test_installer_setup_supports_scope_shortcuts_launch_and_bootstrap():
    root = Path(__file__).resolve().parents[2]
    text = (root / "installer" / "RadioTEDUBroadcastRoomSetup.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=admin" in text
    assert "PrivilegesRequiredOverridesAllowed=dialog" not in text
    assert '#define AppPublisher "RadioTEDU Technologies"' in text
    assert "AppPublisher={#AppPublisher}" in text
    assert "DefaultDirName={commonpf}\\RadioTEDU\\OnAir" in text
    assert "WizardImageFile=assets\\wizard-large.bmp" in text
    assert "WizardSmallImageFile=assets\\wizard-small.bmp" in text
    assert "LicenseFile=..\\LICENSE.md" in text
    assert 'Source: "..\\LICENSE.md"; DestDir: "{app}\\licenses"' in text
    assert 'Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}\\licenses"' in text
    assert 'Name: "desktopicon"' in text
    assert 'Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce' in text
    assert 'Name: "startmenuicon"' in text
    assert '#define ServiceName "RadioTEDU.OnAir.Supervisor"' in text
    assert 'Name: "healthwallshortcut"' not in text
    assert 'Name: "healthwallautostart"' not in text
    assert 'RadioTEDU Health Wall' not in text
    assert 'Name: "{commonstartup}\\RadioTEDU Health Wall"' not in text
    assert 'ConfigureHealthWallStartup.ps1' not in text
    assert "RadioTEDU-OnAir-Agent.exe" not in text
    assert 'Source: "..\\dist\\desktop\\supervisor\\*"' in text
    assert "..\\dist\\desktop\\shell\\*" in text
    assert "EnsureDesktopPrerequisites.ps1" in text
    assert "InstallSupervisorService.ps1" in text
    assert "InstallAudioWatchdog.ps1" in text
    assert "RadioTEDU-AudioWatchdog.ps1" in text
    assert "RadioTEDU OnAir - Audio Watchdog" not in text  # task name is owned by the installer script
    assert "-Action Prepare" in text
    assert "-Action Start" in text
    assert "-Action Install" in text
    assert "-Action Rollback" in text
    assert "-Action Remove" in text
    assert "-RequireServiceIdentity" in text
    assert "-InstallDotNetDesktopRuntime" not in text
    assert "-InstallOllama" not in text
    assert "postinstall" in text.lower()


def test_audio_watchdog_installer_is_independent_and_fail_closed():
    root = Path(__file__).resolve().parents[2]
    installer = (root / "installer" / "InstallAudioWatchdog.ps1").read_text(
        encoding="utf-8"
    )
    watchdog = (root / "tools" / "RadioTEDU-AudioWatchdog.ps1").read_text(
        encoding="utf-8"
    )

    assert 'TaskName = "RadioTEDU OnAir - Audio Watchdog"' in installer
    assert "New-ScheduledTaskTrigger" in installer
    assert "New-ScheduledTaskTrigger -AtStartup" in installer
    assert "FromMinutes(5)" in installer
    assert "[TimeSpan]::MaxValue" in installer
    assert 'UserId "SYSTEM"' in installer
    assert "Export-ScheduledTask" in installer
    assert "Register-ScheduledTask" in installer
    assert "Unregister-ScheduledTask" in installer
    assert "X-RadioTEDU-Watchdog-Token" in watchdog
    assert "Start-Sleep -Seconds 30" in watchdog
    assert "Test-RepairCooldown" in watchdog
    assert "Get-LocalTransportState" in watchdog
    assert "Get-RepairableStationIds" in watchdog
    assert "Test-OriginResponsive" in watchdog
    assert 'Send-Report "origin_unavailable"' in watchdog
    assert "local source and AI restarts suppressed" in watchdog
    assert '"upstream_degraded"' in watchdog
    assert "healthy local sources were not restarted" in watchdog
    assert "duplicate launch refused" in watchdog
    assert "station_ids = @($repairableFailed)" in watchdog
    assert "volumedetect" in watchdog
    assert watchdog.index('"-i",') < watchdog.index('"-t", "8"')
    assert 'ListenerBase = "http://stream.radiotedu.com:11154"' in watchdog
    assert '"-re", "-stats_period", "8"' in watchdog
    assert "$mediaSeconds -ge 7.5" in watchdog
    assert 'Url = "$listenerRoot/rock"' in watchdog
    assert "MaxConcurrentAudioProbes = 4" in watchdog
    assert "Invoke-PublicAudioProbeBatches" in watchdog
    assert "TransportFreshnessSeconds = 5.0" in watchdog
    assert "pending_items -le 0" not in watchdog


def test_one_shot_installer_preserves_watchdog_boot_recovery():
    root = Path(__file__).resolve().parents[2]
    installer = (root / "tools" / "Install-RadioTEDU-OneShot.ps1").read_text(
        encoding="utf-8"
    )

    assert "New-ScheduledTaskTrigger -AtStartup" in installer
    assert "@($periodicTrigger, $startupTrigger)" in installer


def test_official_desktop_bundle_requires_self_contained_publish():
    root = Path(__file__).resolve().parents[2]
    text = (root / "build_desktop_bundle.ps1").read_text(encoding="utf-8")

    assert '[bool]$AllowFrameworkDependentFallback = $false' in text
    assert "-SelfContained $true" in text


def test_desktop_executables_embed_the_radiotedu_application_icon():
    root = Path(__file__).resolve().parents[2]
    icon = root / "app" / "static" / "icons" / "icon.ico"
    assert icon.is_file() and icon.stat().st_size > 0
    for project in (
        root / "desktop" / "src" / "CleanroomRadio.Shell" / "CleanroomRadio.Shell.csproj",
        root / "desktop" / "src" / "CleanroomRadio.ServiceHost" / "CleanroomRadio.ServiceHost.csproj",
        ):
        text = project.read_text(encoding="utf-8")
        assert '<ApplicationIcon>../../../app/static/icons/icon.ico</ApplicationIcon>' in text


def test_clean_install_acceptance_harness_is_elevated_and_fail_closed():
    root = Path(__file__).resolve().parents[2]
    harness = (root / "installer" / "RunRadioTEDUCleanInstallAcceptance.ps1").read_text(
        encoding="utf-8"
    )
    assert "must run from an elevated Administrator PowerShell" in harness
    assert "InstallRoot must be a child of the current user's TEMP directory" in harness
    assert "Service $serviceName already exists; run on a clean disposable VM." in harness
    assert "ProgramData retained as designed" in harness
    assert 'Join-Path "backend" $backendExe' in harness


def test_ci_installer_and_backend_build_use_exact_python_dependency_lock():
    root = Path(__file__).resolve().parents[2]
    lock_path = root / "requirements.lock"
    lock_lines = [
        line.strip()
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lock_lines
    assert all("==" in line for line in lock_lines)
    for required in (
        "fastapi==",
        "pydantic==",
        "python-multipart==",
        "requests==",
        "uvicorn==",
        "websockets==",
    ):
        assert any(line.startswith(required) for line in lock_lines)

    ci = (root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    installer_build = (root / "installer" / "build_setup.ps1").read_text(
        encoding="utf-8"
    )
    backend_build = (root / "build_backend_onefile.ps1").read_text(
        encoding="utf-8"
    )
    assert "pip install --only-binary=:all: -r requirements.lock" in ci
    assert 'python-version: "3.12"' in ci
    assert "python -m pip check" in ci
    assert '..\\requirements.lock"' in installer_build
    assert "--only-binary=:all:" in installer_build
    assert 'Join-Path $root "requirements.lock"' in backend_build
    assert '-m venv $buildVenv' in backend_build
    assert '$env:PYTHONPATH = ""' in backend_build
    assert "-m pip check" in backend_build
    assert "--target $localPythonPackages" not in backend_build
    assert '"pyinstaller-hooks-contrib==2026.6"' in backend_build
    assert "Get-BackendSourceFingerprint" in backend_build
    assert "Backend source changed during packaging" in backend_build
    assert 'Join-Path $stagedOut "build-provenance.json"' in backend_build

    smoke = (root / "tools" / "smoke_stable_backend.ps1").read_text(
        encoding="utf-8"
    )
    assert '"CLEANROOM_TOOLS_DIR"' in smoke
    assert '$env:CLEANROOM_TOOLS_DIR = Join-Path $smokeRoot "tools"' in smoke


def test_prerequisite_bootstrap_detects_per_user_webview2_runtime_via_registry(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "installer" / "EnsureDesktopPrerequisites.ps1"

    command = rf"""
$ErrorActionPreference = 'Stop'
$global:registryPaths = @()
function Get-ItemProperty {{
    param([string]$Path, [string]$Name)
    $global:registryPaths += $Path
    if ($Path -eq 'Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}') {{
        return [pscustomobject]@{{ pv = '126.0.0.0' }}
    }}
    return $null
}}
function Invoke-WebRequest {{
    throw 'bootstrapper should not be downloaded when HKCU runtime is present'
}}
function Start-Process {{
    throw 'bootstrapper should not run when HKCU runtime is present'
}}
& '{script}'
if ($global:registryPaths -notcontains 'Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}') {{
    throw 'HKCU WebView2 runtime registry key was not inspected'
}}
"""

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_prerequisite_bootstrap_detects_per_machine_webview2_runtime_via_registry(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "installer" / "EnsureDesktopPrerequisites.ps1"

    command = rf"""
$ErrorActionPreference = 'Stop'
$global:registryPaths = @()
function Get-ItemProperty {{
    param([string]$Path, [string]$Name)
    $global:registryPaths += $Path
    if ($Path -eq 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}') {{
        return [pscustomobject]@{{ pv = '126.0.0.0' }}
    }}
    return $null
}}
function Invoke-WebRequest {{
    throw 'bootstrapper should not be downloaded when HKLM runtime is present'
}}
function Start-Process {{
    throw 'bootstrapper should not run when HKLM runtime is present'
}}
& '{script}'
if ($global:registryPaths -notcontains 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}') {{
    throw 'HKLM WebView2 runtime registry key was not inspected'
}}
"""

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_build_setup_script_ensures_bundle_and_locates_iscc():
    root = Path(__file__).resolve().parents[2]
    text = (root / "installer" / "build_setup.ps1").read_text(encoding="utf-8")

    assert "build_desktop_bundle.ps1" in text
    assert "generate_brand_assets.ps1" in text
    assert "RadioTEDUBroadcastRoomSetup.iss" in text
    assert "ISCC.exe" in text
    assert '$ReleaseDirectoryName = "setup"' in text
    assert '"RadioTEDU-OnAir.exe"' in text
    assert 'Join-Path $bundleRoot "shell\\$ShellExeName"' in text
    assert 'Join-Path $bundleRoot "supervisor\\$SupervisorExeName"' in text
    assert "RadioTEDU-OnAir-Agent.exe" not in text
    assert "ProductMode" not in text
    assert "InnoSetupCompiler" in text
    assert "INNO_SETUP_COMPILER" in text
    assert "-ExplicitCompiler" in text


def test_build_setup_script_records_exact_installer_path_for_smoke_validation():
    root = Path(__file__).resolve().parents[2]
    text = (root / "installer" / "build_setup.ps1").read_text(encoding="utf-8")

    assert "last_setup_path.txt" in text
    assert '"RadioTEDU-OnAir-Setup"' in text
    assert "Get-FileHash -Path $setupPath -Algorithm SHA256" in text
    assert '"$setupBaseName.sha256"' in text
    assert "Set-Content" in text
    assert '"RadioTEDU-OnAir-Backend.exe"' in text
    assert "..\\dist\\$BackendDistDirectoryName\\$BackendExeName" in text
    assert "..\\build\\$BackendPublishDirectoryName\\$([System.IO.Path]::GetFileNameWithoutExtension($BackendExeName))\\$BackendExeName" in text


def test_build_setup_script_uses_python_resolution_instead_of_hardcoded_py_launcher():
    root = Path(__file__).resolve().parents[2]
    text = (root / "installer" / "build_setup.ps1").read_text(encoding="utf-8")

    assert "Resolve-PythonInstallCommand" in text
    assert "py -3.12 -m pip install" not in text
    assert "backend-publish" in text
    assert "last_build_path.txt" in text


def test_build_setup_script_installs_python_requirements_before_packaging():
    root = Path(__file__).resolve().parents[2]
    text = (root / "installer" / "build_setup.ps1").read_text(encoding="utf-8").lower()

    assert "pip install -r" in text
    assert "requirements.lock" in text
    assert "--only-binary=:all:" in text


def test_installer_source_is_documented_as_open_source():
    root = Path(__file__).resolve().parents[2]
    license_text = (root / "installer" / "LICENSE.md").read_text(encoding="utf-8")
    readme_text = (root / "installer" / "README.md").read_text(encoding="utf-8")
    notices_text = (root / "installer" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    root_license = (root / "LICENSE.md").read_text(encoding="utf-8")

    assert "MIT License" in license_text
    assert "installer source" in license_text.lower()
    assert "does not change the license" in license_text
    assert "open source under" in readme_text.lower()
    assert "Inno Setup" in readme_text
    assert "ISCC.exe" in readme_text
    assert "https://github.com/jrsoftware/issrc" in notices_text
    assert "installer/LICENSE.md" in root_license


def test_prerequisite_bootstrap_downloads_webview2_runtime_when_missing(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "installer" / "EnsureDesktopPrerequisites.ps1"
    bootstrapper = tmp_path / "WebView2Bootstrapper.exe"

    command = rf"""
$ErrorActionPreference = 'Stop'
$global:runtimeInstalled = $false
function Test-WebView2RuntimeInstalled {{
    return $global:runtimeInstalled
}}
function Invoke-WebRequest {{
    param([string]$Uri, [string]$OutFile)
    Set-Content -Path $OutFile -Value "bootstrapper from $Uri" -NoNewline
}}
function Start-Process {{
    param([string]$FilePath, [string]$ArgumentList, [switch]$Wait, [switch]$PassThru, [string]$WindowStyle)
    if (-not (Test-Path $FilePath)) {{
        throw "bootstrapper missing"
    }}
    if ($ArgumentList -notmatch '/silent') {{
        throw "bootstrapper was not launched silently"
    }}
    $global:runtimeInstalled = $true
    $global:invoked = [pscustomobject]@{{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
    }}
    return [pscustomobject]@{{ ExitCode = 0 }}
}}
& '{script}' -RuntimeInstalledCheck {{ $global:runtimeInstalled }} -DownloadAction {{ param($Uri, $OutFile) Set-Content -Path $OutFile -Value "bootstrapper from $Uri" -NoNewline }} -SignatureCheck {{ $true }} -BootstrapperUrl 'https://example.com/webview2.exe' -BootstrapperPath '{bootstrapper}'
if (-not $global:runtimeInstalled) {{
    throw 'runtime not marked installed'
}}
if ($global:invoked.FilePath -ne '{bootstrapper}') {{
    throw 'wrong bootstrapper path'
}}
"""

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_prerequisite_bootstrap_does_not_force_optional_ollama_installation():
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "EnsureDesktopPrerequisites.ps1").read_text(encoding="utf-8")
    installer = (root / "installer" / "RadioTEDUBroadcastRoomSetup.iss").read_text(encoding="utf-8")

    assert "InstallOllama" not in script
    assert "InstallOllama" not in installer
    assert "WebView2" in script



def test_self_contained_bundle_does_not_force_dotnet_desktop_runtime_installation():
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "EnsureDesktopPrerequisites.ps1").read_text(encoding="utf-8")
    bundle = (root / "build_desktop_bundle.ps1").read_text(encoding="utf-8")

    assert "InstallDotNetDesktopRuntime" not in script
    assert '"--self-contained"' in bundle
    assert '"true"' in bundle


def test_supervisor_registration_uses_only_radiotedu_artifacts_and_port():
    root = Path(__file__).resolve().parents[2]
    helper = (root / "installer" / "InstallSupervisorService.ps1").read_text(
        encoding="utf-8"
    )
    radio_installer = (
        root / "installer" / "RadioTEDUBroadcastRoomSetup.iss"
    ).read_text(encoding="utf-8")
    assert "ProductMode" not in helper
    assert '"RadioTEDU-OnAir-Supervisor.exe"' in helper
    assert '"RadioTEDU-OnAir-Backend.exe"' in helper
    assert "$backendPort = 8100" in helper
    assert '"CLEANROOM_DATA_ROOT=$DataRoot"' in helper
    assert '"CLEANROOM_DB_PATH=$(Join-Path $DataRoot \'cleanroom.db\')"' in helper
    assert '"CLEANROOM_USER_CONFIG_ROOT=$DataRoot"' in helper
    assert '"CLEANROOM_JWT_SECRET_FILE=$(Join-Path $DataRoot \'secrets\\jwt-signing.key\')"' in helper
    assert '"CLEANROOM_TOOLS_DIR=$(Join-Path $backendRoot \'tools\')"' in helper
    assert '"CLEANROOM_SKIP_ICECAST_METADATA=0"' in helper
    assert "-PropertyType MultiString" in helper
    assert "-ProductMode" not in radio_installer


def test_installer_provenance_links_source_backend_desktop_and_watchdog_inputs():
    root = Path(__file__).resolve().parents[2]
    build = (root / "installer" / "build_setup.ps1").read_text(encoding="utf-8")

    assert "schema_version = 2" in build
    assert "source_git_commit = $sourceGitCommit" in build
    assert "source_git_tracked_tree_dirty = $sourceGitTrackedTreeDirty" in build
    assert 'throw "Release provenance requires a clean tracked source tree."' in build
    assert "backend_git_commit = [string]$backendProvenance.git_commit" in build
    assert "backend_source_sha256 = [string]$backendProvenance.source_sha256" in build
    assert "backend_executable_sha256" in build
    assert "desktop_shell_sha256" in build
    assert "desktop_supervisor_sha256" in build
    assert "installer_definition_sha256" in build
    assert "supervisor_installer_sha256" in build
    assert "audio_watchdog_installer_sha256" in build
    assert "audio_watchdog_script_sha256" in build


def test_installers_configure_bounded_radiotedu_wer_local_dumps():
    root = Path(__file__).resolve().parents[2]
    helper = (root / "installer" / "ConfigureCrashDumps.ps1").read_text(
        encoding="utf-8"
    )
    radio = (root / "installer" / "RadioTEDUBroadcastRoomSetup.iss").read_text(
        encoding="utf-8"
    )

    assert "Windows Error Reporting\\LocalDumps" in helper
    assert '"DumpCount"' in helper and "-Value 10" in helper
    assert '"DumpType"' in helper and "-Value 1" in helper
    assert "RadioTEDU-OnAir-Backend.exe" in helper
    assert "rtAI-OnAir-Backend.exe" not in helper
    assert "ConfigureCrashDumps.ps1" in radio
    assert "-ProductMode" not in radio
