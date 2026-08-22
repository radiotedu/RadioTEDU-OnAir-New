from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_durable_launcher_prefers_source_and_preserves_packaged_fallback():
    launcher = (ROOT / "tools" / "start_radio_backend.ps1").read_text(
        encoding="utf-8"
    )

    assert '[ValidateSet("Auto", "Source", "Packaged")]' in launcher
    assert 'Join-Path $repoRoot "run_cleanroom.py"' in launcher
    assert 'Join-Path $repoRoot "last_build_path.txt"' in launcher
    assert '$selectedMode = if ($sourceAvailable) { "Source" } else { "Packaged" }' in launcher
    assert '$selectedMode = "Packaged"' in launcher
    assert "Wait-RadioBackendLive" in launcher
    assert "/api/health/live" in launcher


def test_durable_launcher_preserves_migrated_state_secrets_tools_and_isolation():
    launcher = (ROOT / "tools" / "start_radio_backend.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Join-Path $dataRoot "cleanroom.db"' in launcher
    assert 'Join-Path $userRoot "secrets\\jwt-signing.key"' in launcher
    assert '$env:CLEANROOM_TOOLS_DIR = $toolsRoot' in launcher
    assert '$env:CLEANROOM_SKIP_ICECAST_METADATA = "0"' in launcher
    assert '$env:RADIOTEDU_PROCESS_ISOLATED_WORKERS = "1"' in launcher


def test_runtime_wrapper_and_operator_launcher_use_durable_launcher():
    wrapper = (ROOT / "run" / "new-program" / "start-new-program.ps1").read_text(
        encoding="utf-8"
    )
    operator = (ROOT / "tools" / "launch_new_program.ps1").read_text(
        encoding="utf-8"
    )

    assert 'tools\\start_radio_backend.ps1' in wrapper
    assert 'tools\\start_radio_backend.ps1' in operator
    assert "/api/health/live" in operator
    assert "RadioTEDU-OnAir-Backend.exe" not in operator
