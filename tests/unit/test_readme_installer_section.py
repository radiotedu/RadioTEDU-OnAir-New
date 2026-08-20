from pathlib import Path


def test_readme_documents_windows_installer_flow():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    assert "windows installer" in lowered
    assert "setup.exe" in lowered
    assert "system tray" in lowered
    assert "current user" in lowered
    assert "all users" in lowered
    assert "tray menu" in lowered


def test_smoke_script_mentions_desktop_bundle_and_installer_artifacts():
    script = (Path(__file__).resolve().parents[2] / "smoke_test_desktop_bundle.ps1").read_text(
        encoding="utf-8"
    )

    assert '$ShellExeName = "RadioTEDU-OnAir.exe"' in script
    assert 'dist\\desktop\\shell\\$ShellExeName' in script
    assert "release\\setup" in script
    assert "last_setup_path.txt" in script
    assert "last_build_path.txt" in script
    assert "Resolve-RecordedBackendPath" in script


def test_readme_documents_smoke_validation_for_installer_and_backend():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    ).lower()

    assert "smoke_test_desktop_bundle.ps1" in text
    assert "last_setup_path.txt" in text
    assert "last_build_path.txt" in text
    assert "cleanroom_open_panel=0" in text
    assert "free loopback port" in text
