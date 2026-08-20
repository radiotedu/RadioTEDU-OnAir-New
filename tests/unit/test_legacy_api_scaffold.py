from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_legacy_scaffold_endpoints_exist():
    client = TestClient(app)
    stations = client.get("/api/stations")
    assert stations.status_code == 200
    payload = stations.json()
    assert isinstance(payload.get("stations"), list)
    assert len(payload["stations"]) >= 1
    assert client.get("/api/settings/system").status_code == 200


def test_station_settings_endpoint_tolerates_missing_or_invalid_station_id():
    client = TestClient(app)
    r1 = client.get("/api/settings/station")
    assert r1.status_code == 200
    p1 = r1.json()
    assert isinstance(p1.get("settings"), dict)

    r2 = client.get("/api/settings/station", params={"station_id": "undefined"})
    assert r2.status_code == 200
    p2 = r2.json()
    assert isinstance(p2.get("settings"), dict)


def test_invalid_station_id_query_is_sanitized_for_required_int_routes():
    client = TestClient(app)
    playlists = client.get("/api/playlists", params={"station_id": "undefined"})
    assert playlists.status_code == 200
    assert isinstance(playlists.json(), list)


def test_invalid_station_id_falls_back_to_active_station():
    client = TestClient(app)
    created = client.post("/api/stations", json={"name": "Fallback Target"})
    assert created.status_code == 200
    station_id = int(created.json()["id"])

    set_active = client.post("/api/stations/active", json={"station_id": station_id})
    assert set_active.status_code == 200

    playlist = client.post(
        "/api/playlists",
        json={"station_id": station_id, "name": "Fallback Playlist", "description": ""},
    )
    assert playlist.status_code == 200
    playlist_id = int(playlist.json()["playlist_id"])

    listed = client.get("/api/playlists", params={"station_id": "undefined"})
    assert listed.status_code == 200
    rows = listed.json()
    assert any(int(row.get("id", 0)) == playlist_id for row in rows)


def test_list_stations_does_not_recreate_rows_on_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM stations")
        conn.commit()
    finally:
        conn.close()

    client = TestClient(app)
    listed = client.get("/api/stations")
    assert listed.status_code == 200
    assert listed.json() == {"stations": []}

    verify = get_connection()
    try:
        remaining = verify.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    finally:
        verify.close()
    assert remaining == 0


def test_invalid_source_station_query_is_sanitized():
    client = TestClient(app)
    res = client.get(
        "/api/tracks",
        params={
            "station_id": 1,
            "library_scope": "station",
            "source_station_id": "undefined",
            "page": 1,
            "per_page": 10,
        },
    )
    assert res.status_code == 200
    payload = res.json()
    assert isinstance(payload.get("tracks"), list)


def test_station_settings_put_accepts_flat_payload():
    client = TestClient(app)
    write = client.put(
        "/api/settings/station",
        json={
            "station_id": 1,
            "station_timezone": "UTC",
            "output_gain_db": 1.5,
            "output_mode": "speaker",
            "speaker_monitor_enabled": True,
            "icecast_port": 8000,
            "icecast_public": False,
        },
    )
    assert write.status_code == 200
    payload = write.json()
    assert payload.get("ok") is True
    assert int(payload.get("station_id", 0)) >= 1

    read = client.get("/api/settings/station", params={"station_id": payload["station_id"]})
    assert read.status_code == 200
    settings = read.json().get("settings") or {}
    assert settings.get("output_mode") == "speaker"
    assert settings.get("speaker_monitor_enabled") is True
    assert int(settings.get("icecast_port")) == 8000


def test_station_settings_post_is_supported_for_legacy_clients():
    client = TestClient(app)
    write = client.post(
        "/api/settings/station",
        json={
            "station_id": 1,
            "output_mode": "icecast",
            "speaker_monitor_enabled": False,
            "icecast_port": 8000,
        },
    )
    assert write.status_code == 200
    payload = write.json()
    assert payload.get("ok") is True
    assert int(payload.get("station_id", 0)) >= 1


def test_system_settings_put_accepts_flat_payload():
    client = TestClient(app)
    write = client.put(
        "/api/settings/system",
        json={
            "ui_language": "tr-TR",
            "default_crossfade_seconds": 4.25,
            "operation_logs_enabled": True,
            "auto_scan_on_startup": False,
        },
    )
    assert write.status_code == 200

    read = client.get("/api/settings/system")
    assert read.status_code == 200
    settings = read.json().get("settings") or {}
    assert settings.get("ui_language") == "tr-TR"
    assert float(settings.get("default_crossfade_seconds")) == 4.25
    assert settings.get("operation_logs_enabled") is True
    assert settings.get("auto_scan_on_startup") is False
    assert int(settings.get("active_station_id")) >= 1
    assert int(settings.get("speaker_monitor_station_id")) >= 1
