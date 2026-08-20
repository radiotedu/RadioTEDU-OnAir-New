from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "Install-RadioTEDU-OneShot.ps1"


def test_one_shot_installer_has_complete_durable_ownership_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "Assert-Administrator",
        "RadioTEDU.OnAir.Supervisor",
        "RadioTEDU.AIStreams",
        "RadioTEDU.SharedAI",
        "RadioTEDUVotingRadio",
        "RadioTEDU.JukeLocalMediaAgent",
        "InstallAudioWatchdog.ps1",
        "commission_quality_outputs.py",
        "run_ai_quality_supervisor.py",
        "'start=' 'delayed-auto'",
        "'actions=' 'restart/5000/restart/15000/restart/60000'",
        "machine DPAPI",
        "Sort-Object LastWriteTime -Descending",
        "FromMinutes(8)",
        "Wait-OnAirReady",
        "Wait-ProcessCommandLine 'run_ai_quality_supervisor\\.py'",
        "[Text.UTF8Encoding]::new($false)",
        "one-shot-install-state.json",
        "Move-Item -LiteralPath $temporaryState -Destination $statePath -Force",
    ):
        assert token in text


def test_one_shot_disables_only_known_old_startup_owners_and_preserves_files():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "RadioTEDU OnAir Metadata Refresh" in text
    assert "RadioTEDU-OnAir-Agent.exe" in text
    assert "Move-Item -LiteralPath $OldCommonStartup" in text
    assert "Remove-Item" not in text
    assert "RadioTEDU-OnAir-Radio" not in str(ROOT.parent / "RadioTEDU-OnAir")


def test_one_shot_never_embeds_or_prints_source_credentials():
    text = SCRIPT.read_text(encoding="utf-8").lower()
    assert "source_password=" not in text
    assert "icecast_password=" not in text
    assert "get_secret(" not in text
    assert "credential://" not in text
