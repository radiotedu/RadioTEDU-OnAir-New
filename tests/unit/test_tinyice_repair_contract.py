from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "Repair-TinyIce-Origin.ps1"
).read_text(encoding="utf-8")


def test_tinyice_repair_is_scoped_and_fail_closed():
    assert "SupportsShouldProcess = $true" in SCRIPT
    assert "Restart-Service -Name $owner -Force" in SCRIPT
    assert "Stop-ScheduledTask" in SCRIPT
    assert "Start-ScheduledTask" in SCRIPT
    assert "ownership is ambiguous" in SCRIPT
    assert "no process was killed" in SCRIPT
    assert "Restart-Computer" not in SCRIPT
    assert "shutdown.exe" not in SCRIPT
    assert "Stop-Process" not in SCRIPT


def test_tinyice_repair_requires_http_verification():
    assert "Test-TinyIceHttp" in SCRIPT
    assert "Wait-TinyIceHttp" in SCRIPT
    assert "verified_http = $true" in SCRIPT
    assert "origin_already_responsive" in SCRIPT
    assert "GetResponseStream" in SCRIPT
    assert ".Read($buffer, 0, 1) -gt 0" in SCRIPT
