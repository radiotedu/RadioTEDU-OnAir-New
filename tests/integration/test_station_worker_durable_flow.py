from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.queue_repo import QueueRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.running: dict[int, bool] = {}

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float | None = None,
    ):
        self.running[int(station_id)] = True
        return {"station_id": int(station_id), "running": True}

    def status(self, station_id: int):
        sid = int(station_id)
        return {
            "station_id": sid,
            "running": bool(self.running.get(sid, False)),
            "branch_health": {"icecast": False, "local": bool(self.running.get(sid, False))},
            "required_outputs": {"icecast": False, "local": True},
        }


def test_worker_claims_item_then_marks_manual_item_done_after_runtime_stops(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (id, title, artist, musicbrainz_recordingid, file_path, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (77, "Song", "Artist", "", "C:/music/song.mp3", 1),
    )
    conn.commit()
    queue = QueueRepository(conn)
    queue.enqueue(1, 77, "w-item-1")
    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, worker_id="worker-1", runtime_registry=runtime)

    first = worker.process_once()
    assert first["source"] == "manual"
    cur.execute("SELECT status FROM queue_items WHERE station_id=1 ORDER BY id ASC LIMIT 1")
    assert cur.fetchone()["status"] == "playing"

    second = worker.process_once()
    assert second == {"source": "playing", "reason": "track_in_progress"}
    cur.execute("SELECT status FROM queue_items WHERE station_id=1 ORDER BY id ASC LIMIT 1")
    assert cur.fetchone()["status"] == "playing"

    runtime.running[1] = False
    third = worker.process_once()
    assert third["source"] == "none"
    cur.execute("SELECT status FROM queue_items WHERE station_id=1 ORDER BY id ASC LIMIT 1")
    assert cur.fetchone()["status"] == "done"
