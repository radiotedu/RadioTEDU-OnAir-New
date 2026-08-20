import shutil
import subprocess
from pathlib import Path

from scripts.release_bundle import build_release_manifest


def test_release_manifest_contains_backend_and_desktop_artifacts():
    manifest = build_release_manifest("1.0.2")

    assert manifest["version"] == "1.0.2"

    artifacts = {item["name"]: item["path"] for item in manifest["artifacts"]}

    assert artifacts["RadioTEDU-OnAir-Backend.exe"] == "dist/backend/RadioTEDU-OnAir-Backend.exe"
    assert artifacts["RadioTEDU-OnAir-Supervisor.exe"] == "dist/desktop/supervisor/RadioTEDU-OnAir-Supervisor.exe"
    assert artifacts["RadioTEDU-OnAir.exe"] == "dist/desktop/shell/RadioTEDU-OnAir.exe"


def test_release_manifest_points_shell_artifact_at_shell_subdir():
    manifest = build_release_manifest("1.0.2")
    artifacts = {item["name"]: item["path"] for item in manifest["artifacts"]}

    assert artifacts["RadioTEDU-OnAir.exe"] == "dist/desktop/shell/RadioTEDU-OnAir.exe"


def test_bundle_scripts_pin_release_layout_and_local_dotnet():
    root = Path(__file__).resolve().parents[2]

    bundle_script = (root / "build_desktop_bundle.ps1").read_text(encoding="utf-8")
    portable_script = (root / "package_portable_release.ps1").read_text(encoding="utf-8")

    assert "scripts\\ensure_dotnet.ps1" in bundle_script
    assert '.dotnet"' in bundle_script
    assert "shell-publish" in bundle_script
    assert "shellBundleDir" in bundle_script
    assert 'Join-Path $shellBundleDir' in bundle_script
    assert '$DesktopDistDirectoryName = "desktop"' in bundle_script
    assert "dist\\backend\\RadioTEDU-OnAir-Backend.exe" in portable_script
    assert '"--self-contained"' in bundle_script
    assert '"true"' in bundle_script
    assert "framework-dependent" in bundle_script
    assert '"last_build_path.txt"' in bundle_script
    assert '"last_rtai_build_path.txt"' not in bundle_script
    assert "Recorded latest backend path" in bundle_script


def test_backend_bundle_script_builds_console_backend_bundle_into_backend_dist():
    root = Path(__file__).resolve().parents[2]

    backend_script = (root / "build_backend_onefile.ps1").read_text(encoding="utf-8")

    assert "--console" in backend_script
    assert "--noconsole" not in backend_script
    assert "launched hidden by $SupervisorExeName" in backend_script
    assert '.\\dist\\backend' in backend_script
    assert 'ffplay_sha256' in backend_script


def test_desktop_bundle_promotes_entire_timestamped_backend_bundle():
    root = Path(__file__).resolve().parents[2]
    script = (root / "build_desktop_bundle.ps1").read_text(encoding="utf-8")

    assert "Copy-PublishTree -Source $sourceBundle -Destination $canonicalBundle" in script
    assert 'Copy-Item -Path $lastBuiltExe -Destination $CanonicalPath -Force' not in script


def test_desktop_bundle_falls_back_to_timestamped_backend_when_last_build_path_is_truncated(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "bundle"
    script_root.mkdir()

    shutil.copy2(root / "build_desktop_bundle.ps1", script_root / "build_desktop_bundle.ps1")

    scripts_dir = script_root / "scripts"
    scripts_dir.mkdir()

    fake_dotnet = script_root / "fake-dotnet.ps1"
    fake_dotnet.write_text(
        """
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

if ($Args.Count -ge 1 -and $Args[0] -eq 'publish') {
    $projectPath = $Args[1]
    $outputIndex = [Array]::IndexOf($Args, '--output')
    $outputDir = $Args[$outputIndex + 1]
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    $exeName = if ($projectPath -like '*Shell*') { 'CleanroomRadio.Shell.exe' } else { 'CleanroomRadio.ServiceHost.exe' }
    Set-Content -Path (Join-Path $outputDir $exeName) -Value $exeName -NoNewline
    exit 0
}

if ($Args.Count -ge 1 -and $Args[0] -eq '--version') {
    Write-Output '8.0.415'
    exit 0
}

exit 0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    (scripts_dir / "ensure_dotnet.ps1").write_text(
        f"Write-Output '{fake_dotnet}'\n",
        encoding="utf-8",
    )

    backend_build_script = script_root / "build_backend_onefile.ps1"
    backend_build_script.write_text(
        """
param()

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$distRoot = Join-Path $root "dist"
$timestampedDir = Join-Path $distRoot "backend-20260327-101500"
New-Item -ItemType Directory -Force -Path $timestampedDir | Out-Null
Set-Content -Path (Join-Path $timestampedDir "RadioTEDU-OnAir-Backend.exe") -Value "backend" -NoNewline
Set-Content -Path (Join-Path $root "last_build_path.txt") -Value "C" -NoNewline
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_root / "build_desktop_bundle.ps1"),
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (script_root / "dist" / "backend" / "RadioTEDU-OnAir-Backend.exe").exists()
    assert (script_root / "dist" / "desktop" / "supervisor" / "RadioTEDU-OnAir-Supervisor.exe").exists()
    assert (
        script_root / "dist" / "desktop" / "shell" / "RadioTEDU-OnAir.exe"
    ).exists()


def test_desktop_bundle_falls_back_to_timestamped_backend_when_last_build_path_is_empty(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "bundle"
    script_root.mkdir()

    shutil.copy2(root / "build_desktop_bundle.ps1", script_root / "build_desktop_bundle.ps1")

    scripts_dir = script_root / "scripts"
    scripts_dir.mkdir()

    fake_dotnet = script_root / "fake-dotnet.ps1"
    fake_dotnet.write_text(
        """
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

if ($Args.Count -ge 1 -and $Args[0] -eq 'publish') {
    $projectPath = $Args[1]
    $outputIndex = [Array]::IndexOf($Args, '--output')
    $outputDir = $Args[$outputIndex + 1]
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    $exeName = if ($projectPath -like '*Shell*') { 'CleanroomRadio.Shell.exe' } else { 'CleanroomRadio.ServiceHost.exe' }
    Set-Content -Path (Join-Path $outputDir $exeName) -Value $exeName -NoNewline
    exit 0
}

if ($Args.Count -ge 1 -and $Args[0] -eq '--version') {
    Write-Output '8.0.415'
    exit 0
}

exit 0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    (scripts_dir / "ensure_dotnet.ps1").write_text(
        f"Write-Output '{fake_dotnet}'\n",
        encoding="utf-8",
    )

    backend_build_script = script_root / "build_backend_onefile.ps1"
    backend_build_script.write_text(
        """
param()

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$distRoot = Join-Path $root "dist"
$timestampedDir = Join-Path $distRoot "backend-20260327-101600"
New-Item -ItemType Directory -Force -Path $timestampedDir | Out-Null
Set-Content -Path (Join-Path $timestampedDir "RadioTEDU-OnAir-Backend.exe") -Value "backend" -NoNewline
Set-Content -Path (Join-Path $root "last_build_path.txt") -Value "" -NoNewline
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_root / "build_desktop_bundle.ps1"),
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (script_root / "dist" / "backend" / "RadioTEDU-OnAir-Backend.exe").exists()
    assert (script_root / "dist" / "desktop" / "supervisor" / "RadioTEDU-OnAir-Supervisor.exe").exists()
    assert (
        script_root / "dist" / "desktop" / "shell" / "RadioTEDU-OnAir.exe"
    ).exists()


def test_desktop_bundle_prefers_fresh_backend_marker_over_stale_canonical_backend(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "bundle"
    script_root.mkdir()

    shutil.copy2(root / "build_desktop_bundle.ps1", script_root / "build_desktop_bundle.ps1")

    scripts_dir = script_root / "scripts"
    scripts_dir.mkdir()

    fake_dotnet = script_root / "fake-dotnet.ps1"
    fake_dotnet.write_text(
        """
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

if ($Args.Count -ge 1 -and $Args[0] -eq 'publish') {
    $projectPath = $Args[1]
    $outputIndex = [Array]::IndexOf($Args, '--output')
    $outputDir = $Args[$outputIndex + 1]
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    $exeName = if ($projectPath -like '*Shell*') { 'CleanroomRadio.Shell.exe' } else { 'CleanroomRadio.ServiceHost.exe' }
    Set-Content -Path (Join-Path $outputDir $exeName) -Value $exeName -NoNewline
    exit 0
}

if ($Args.Count -ge 1 -and $Args[0] -eq '--version') {
    Write-Output '8.0.415'
    exit 0
}

exit 0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    (scripts_dir / "ensure_dotnet.ps1").write_text(
        f"Write-Output '{fake_dotnet}'\n",
        encoding="utf-8",
    )

    backend_build_script = script_root / "build_backend_onefile.ps1"
    backend_build_script.write_text(
        """
param()

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$canonicalDir = Join-Path $root "dist\\backend"
$freshDir = Join-Path $root "dist\\backend-20260327-170000"
New-Item -ItemType Directory -Force -Path $canonicalDir, $freshDir | Out-Null
Set-Content -Path (Join-Path $canonicalDir "RadioTEDU-OnAir-Backend.exe") -Value "stale-backend" -NoNewline
Set-Content -Path (Join-Path $freshDir "RadioTEDU-OnAir-Backend.exe") -Value "fresh-backend" -NoNewline
Set-Content -Path (Join-Path $root "last_build_path.txt") -Value (Join-Path $freshDir "RadioTEDU-OnAir-Backend.exe") -NoNewline
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_root / "build_desktop_bundle.ps1"),
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (script_root / "dist" / "backend" / "RadioTEDU-OnAir-Backend.exe").read_text(encoding="utf-8") == "fresh-backend"
