import os
import shutil
import subprocess
from pathlib import Path


def test_smoke_script_reads_exact_installer_path_marker():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert "last_setup_path.txt" in script
    assert "Resolve-RecordedInstallerPath" in script
    assert "last_build_path.txt" in script
    assert "Resolve-RecordedBackendPath" in script
    assert "Installer marker" in script


def test_smoke_script_rejects_generic_unbranded_desktop_icons():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Assert-RadioTeduIcon" in script
    assert "ExtractAssociatedIcon" in script
    assert "$radioTeduRedPixels -lt 20" in script
    assert (
        'Assert-RadioTeduIcon -ExecutablePath $shellPath -Label "Desktop shell artifact"'
        in script
    )


def test_smoke_script_sets_backend_env_via_process_start_info():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert "System.Diagnostics.ProcessStartInfo" in script
    assert 'EnvironmentVariables["CLEANROOM_OPEN_PANEL"]' in script
    assert 'EnvironmentVariables["CLEANROOM_HOST"]' in script
    assert 'EnvironmentVariables["CLEANROOM_PORT"]' in script
    assert 'EnvironmentVariables["CLEANROOM_DB_PATH"]' in script
    assert 'EnvironmentVariables["CLEANROOM_DATA_ROOT"]' in script
    assert 'EnvironmentVariables["CLEANROOM_USER_CONFIG_ROOT"]' in script
    assert 'EnvironmentVariables["CLEANROOM_DISABLE_LIBRARY_WATCHER"]' in script
    assert 'EnvironmentVariables["CLEANROOM_SKIP_STARTUP_AI"]' in script
    assert 'EnvironmentVariables["CLEANROOM_SKIP_WORKER_AUTOSTART"]' in script


def test_smoke_script_seeds_local_tools_dir_and_cleans_process_tree():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert "System.Diagnostics.ProcessStartInfo" in script
    assert 'EnvironmentVariables["CLEANROOM_OPEN_PANEL"]' in script
    assert "Get-FreeTcpPort" in script
    assert "Assert-TcpPortAvailable" in script
    assert "Start-BackendProcess" in script
    assert script.count("Seed-SmokeToolsDir -ToolsDir $toolsDir") == 1
    assert script.index("Seed-SmokeToolsDir -ToolsDir $toolsDir") < script.index(
        "while ($attempt -lt $maxAttempts)"
    )


def test_smoke_script_refuses_to_run_when_backend_port_is_already_occupied(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "bundle"
    script_root.mkdir()

    smoke_script = root / "smoke_test_desktop_bundle.ps1"
    temp_smoke_script = script_root / "smoke_test_desktop_bundle.ps1"
    temp_smoke_script.write_text(smoke_script.read_text(encoding="utf-8"), encoding="utf-8")

    backend_path = script_root / "dist" / "backend" / "RadioTEDU-OnAir-Backend.exe"
    backend_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        backend_path,
    )

    shell_path = script_root / "dist" / "desktop" / "shell" / "RadioTEDU-OnAir.exe"
    shell_path.parent.mkdir(parents=True, exist_ok=True)
    shell_path.write_text("shell", encoding="utf-8")

    installer_path = script_root / "release" / "setup" / "RadioTEDU-OnAir-Setup-1.0.2.exe"
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    installer_path.write_text("installer", encoding="utf-8")
    (installer_path.parent / "last_setup_path.txt").write_text(
        str(installer_path),
        encoding="utf-8",
    )
    (script_root / "last_build_path.txt").write_text(str(backend_path), encoding="utf-8")

    command = rf"""
$ErrorActionPreference = 'Stop'
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$occupiedPort = ($listener.LocalEndpoint).Port
try {{
    & '{temp_smoke_script}' -Root '{script_root}' -BackendPort $occupiedPort
}}
finally {{
    $listener.Stop()
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
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Backend port is already in use" in (result.stdout + result.stderr)


def test_smoke_script_defaults_to_auto_allocated_backend_port():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert "[int]$BackendPort = 0" in script
    assert "Get-FreeTcpPort" in script
    assert "if ($BackendPort -le 0)" in script


def test_smoke_script_defaults_to_free_loopback_port_when_another_port_is_occupied(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "bundle"
    script_root.mkdir()

    smoke_script = root / "smoke_test_desktop_bundle.ps1"
    temp_smoke_script = script_root / "smoke_test_desktop_bundle.ps1"
    temp_smoke_script.write_text(smoke_script.read_text(encoding="utf-8"), encoding="utf-8")

    backend_path = script_root / "dist" / "backend" / "RadioTEDU-OnAir-Backend.exe"
    backend_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        backend_path,
    )

    shell_path = script_root / "dist" / "desktop" / "shell" / "RadioTEDU-OnAir.exe"
    shell_path.parent.mkdir(parents=True, exist_ok=True)
    shell_path.write_text("shell", encoding="utf-8")

    installer_path = script_root / "release" / "setup" / "RadioTEDU-OnAir-Setup-1.0.2.exe"
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    installer_path.write_text("installer", encoding="utf-8")
    (installer_path.parent / "last_setup_path.txt").write_text(
        str(installer_path),
        encoding="utf-8",
    )
    (script_root / "last_build_path.txt").write_text(str(backend_path), encoding="utf-8")

    command = rf"""
$ErrorActionPreference = 'Stop'
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$occupiedPort = (($listener.LocalEndpoint).Port)
$global:capturedUri = $null
function Invoke-RestMethod {{
    param([string]$Uri)
    $global:capturedUri = $Uri
    return [pscustomobject]@{{ status = 'ok' }}
}}
try {{
    $smokeError = $null
    try {{
        & '{temp_smoke_script}' -Root '{script_root}'
    }}
    catch {{
        $smokeError = $_
    }}
}}
finally {{
    $listener.Stop()
}}
if ($global:capturedUri -match ":$occupiedPort/") {{
    throw "smoke script targeted the occupied port: $global:capturedUri"
}}
if (-not $global:capturedUri) {{
    throw "smoke script never probed an auto-allocated backend port"
}}
if ($smokeError -and $smokeError.Exception.Message -notmatch "embedded RadioTEDU logo") {{
    throw $smokeError
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
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
