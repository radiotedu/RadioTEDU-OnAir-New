from fastapi.testclient import TestClient

from app.main import app


def test_system_settings_returns_full_cleanroom_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    client = TestClient(app)
    res = client.get("/api/settings/system")

    assert res.status_code == 200
    settings = res.json()["settings"]
    assert settings["ui_language"] == "en-US"
    assert settings["default_crossfade_seconds"] == 3.0
    assert settings["operation_logs_enabled"] is True
    assert settings["auto_scan_on_startup"] is False
    assert settings["display_brand_name"] == "RadioTEDU OnAir"
    assert settings["active_station_id"] == 1
    assert settings["speaker_monitor_station_id"] == 1
