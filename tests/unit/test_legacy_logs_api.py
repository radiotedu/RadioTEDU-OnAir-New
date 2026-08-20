from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_legacy_logs_endpoint_returns_list():
    c = TestClient(app)
    res = c.get("/api/logs", params={"station_id": 1})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_legacy_logs_scope_play_returns_recent_play_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    c = TestClient(app)

    created = c.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Played Song",
            "artist": "Played Artist",
            "track_type": "music",
            "duration": 210,
            "file_path": "C:/music/played-song.mp3",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json()["track_id"])

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, enqueued_at, started_at, finished_at, dedupe_key) "
        "VALUES (?, ?, ?, 'done', ?, ?, ?, ?)",
        (
            1,
            track_id,
            1,
            "2026-03-01 11:00:00",
            "2026-03-01 11:00:05",
            "2026-03-01 11:03:35",
            "legacy-logs-play",
        ),
    )
    conn.commit()

    res = c.get(
        "/api/logs",
        params={"station_id": 1, "scope": "play", "page": 1, "per_page": 10},
    )
    assert res.status_code == 200
    payload = res.json()
    assert isinstance(payload.get("logs"), list)
    assert payload["logs"]
    row = payload["logs"][0]
    assert row["log_type"] == "play"
    assert row["title"] == "Played Song"
    assert row["artist"] == "Played Artist"


def test_legacy_logs_export_returns_csv_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    c = TestClient(app)

    res = c.get(
        "/api/logs/export",
        params={
            "station_id": 1,
            "scope": "all",
            "date_from": "2026-03-01",
            "date_to": "2026-03-13",
            "format": "csv",
        },
    )

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment;" in res.headers.get("content-disposition", "")
    assert "played_at" in res.text
