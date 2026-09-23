from fastapi.testclient import TestClient

from app.main import app


def test_system_settings_returns_full_cleanroom_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))

    client = TestClient(app)
    res = client.get("/api/settings/system")

    assert res.status_code == 200
    settings = res.json()["settings"]
    assert settings["ui_language"] == "en-US"
    assert settings["default_crossfade_seconds"] == 5.0
    assert settings["operation_logs_enabled"] is True
    assert settings["auto_scan_on_startup"] is False
    assert settings["display_brand_name"] == "RadioTEDU OnAir"
    assert settings["active_station_id"] == 1
    assert settings["speaker_monitor_station_id"] == 1


def test_system_crossfade_setting_is_bounded_and_reads_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    client = TestClient(app)

    saved = client.put("/api/settings/system", json={"default_crossfade_seconds": 4.5})
    assert saved.status_code == 200
    assert saved.json()["settings"]["default_crossfade_seconds"] == 4.5

    for invalid in (-0.5, 30.5, "nan", "inf", "invalid"):
        response = client.put(
            "/api/settings/system",
            json={"default_crossfade_seconds": invalid},
        )
        assert response.status_code == 422

    unchanged = client.get("/api/settings/system")
    assert unchanged.status_code == 200
    assert unchanged.json()["settings"]["default_crossfade_seconds"] == 4.5
