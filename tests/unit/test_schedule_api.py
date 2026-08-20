from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_schedule_items_create_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (id, title, artist, musicbrainz_recordingid, file_path) VALUES (?, ?, ?, ?, ?)",
        (701, "ScheduleTrack", "DJ", "", "C:/music/schedule-track.mp3"),
    )
    conn.commit()

    client = TestClient(app)
    create_res = client.post(
        "/api/schedule/items",
        json={
            "station_id": 7,
            "track_id": 701,
            "play_at": "2000-01-01 00:00:00",
            "window_end": "2099-01-01 00:00:00",
        },
    )
    assert create_res.status_code == 200
    assert create_res.json()["ok"] is True

    list_res = client.get("/api/schedule/items", params={"station_id": 7, "limit": 10})
    assert list_res.status_code == 200
    payload = list_res.json()
    assert payload["station_id"] == 7
    assert payload["items"]
    assert int(payload["items"][0]["track_id"]) == 701
