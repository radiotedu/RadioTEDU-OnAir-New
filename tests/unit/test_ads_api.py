from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_ads_items_create_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (6, 'Ads Test')")
    cur.execute(
        "INSERT INTO tracks (id, station_id, title, artist, track_type, musicbrainz_recordingid, file_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (601, 6, "AdTrack", "Brand", "ad", "", "C:/music/ad-track.mp3"),
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (6, 'hourly_ad_enabled', 'true') "
        "ON CONFLICT(station_id, key) DO UPDATE SET value='true'"
    )
    conn.commit()

    client = TestClient(app)
    create_res = client.post(
        "/api/ads/items",
        json={
            "station_id": 6,
            "track_id": 601,
            "due_at": "2000-01-01 00:00:00",
            "priority": 3,
        },
    )
    assert create_res.status_code == 200
    assert create_res.json()["ok"] is True

    list_res = client.get("/api/ads/items", params={"station_id": 6, "limit": 10})
    assert list_res.status_code == 200
    payload = list_res.json()
    assert payload["station_id"] == 6
    assert payload["items"]
    assert int(payload["items"][0]["track_id"]) == 601
