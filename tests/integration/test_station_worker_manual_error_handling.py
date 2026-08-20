from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.queue_repo import QueueRepository


class _RaisingRuntimeRegistry:
    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float = 0.0,
    ):
        raise RuntimeError("runtime start failed")


def test_worker_marks_failed_when_track_uri_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    QueueRepository(conn).enqueue(station_id=1, track_id=123, dedupe_key="missing-track")

    worker = StationWorker(station_id=1, worker_id="worker-1")
    out = worker.process_once()

    assert out["source"] == "manual"
    assert out["reason"] == "track_missing"
    cur = conn.cursor()
    cur.execute("SELECT status FROM queue_items WHERE station_id=1 ORDER BY id ASC LIMIT 1")
    assert cur.fetchone()["status"] == "failed"


def test_worker_marks_failed_and_clears_state_when_runtime_start_raises(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (id, title, artist, musicbrainz_recordingid, file_path) VALUES (?, ?, ?, ?, ?)",
        (99, "Song", "Artist", "", "C:/music/song.mp3"),
    )
    conn.commit()
    QueueRepository(conn).enqueue(station_id=1, track_id=99, dedupe_key="runtime-fail")

    worker = StationWorker(
        station_id=1,
        worker_id="worker-1",
        runtime_registry=_RaisingRuntimeRegistry(),
    )

    try:
        worker.process_once()
        assert False, "expected runtime start failure"
    except RuntimeError:
        pass

    cur.execute("SELECT status FROM queue_items WHERE station_id=1 ORDER BY id ASC LIMIT 1")
    assert cur.fetchone()["status"] == "failed"
    cur.execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=1 LIMIT 1"
    )
    row = cur.fetchone()
    assert row["current_source"] == "none"
    assert row["current_item_id"] is None
