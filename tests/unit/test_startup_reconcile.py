from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.repositories.queue_repo import QueueRepository


def test_app_startup_reconciles_stale_playing_queue_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    queue = QueueRepository(conn)
    item_id = queue.enqueue(1, 42, "startup-stale-item")
    queue.mark_playing(item_id)

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200

    check_conn = get_connection()
    cur = check_conn.cursor()
    cur.execute("SELECT status FROM queue_items WHERE id=?", (item_id,))
    row = cur.fetchone()
    assert row is not None
    assert row["status"] == "pending"


def test_app_startup_backfills_missing_track_durations(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setattr("app.main.bootstrap_dependencies", lambda: {})
    monkeypatch.setattr("app.main._autostart_station_worker_loops", lambda _conn: None)
    init_db()
    conn = get_connection()

    audio_file = tmp_path / "startup-backfill.mp3"
    audio_file.write_bytes(b"ID3")

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, file_path, duration, is_active, track_type) "
        "VALUES (?, ?, ?, ?, 0, 1, 'music')",
        (1, "Startup Backfill", "Tester", str(audio_file)),
    )
    track_id = int(cur.lastrowid)
    conn.commit()

    monkeypatch.setattr(
        "app.engine.playout_state.audio_processing.probe_duration",
        lambda file_path, **_kwargs: 301.5 if str(file_path) == str(audio_file) else 0.0,
    )

    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200

    check_conn = get_connection()
    cur = check_conn.cursor()
    cur.execute("SELECT duration FROM tracks WHERE id=?", (track_id,))
    row = cur.fetchone()
    assert row is not None
    assert float(row["duration"] or 0.0) == 301.5
