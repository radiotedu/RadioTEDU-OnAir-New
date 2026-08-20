from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.queue_repo import QueueRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.starts = []

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float = 0.0,
    ):
        self.starts.append(
            {
                "station_id": station_id,
                "input_uri": input_uri,
                "stream_title": stream_title,
                "stream_artist": stream_artist,
            }
        )
        return {"station_id": station_id, "running": True}


def test_worker_falls_back_to_filename_for_missing_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO stations (id, name) VALUES (?, ?)",
        (21, "Metadata Test"),
    )
    cur.execute(
        "INSERT INTO tracks "
        "(id, station_id, title, artist, musicbrainz_recordingid, file_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (901, 21, "", "", "", "C:/music/My%20Song.mp3"),
    )
    conn.commit()

    QueueRepository(conn).enqueue(station_id=21, track_id=901, dedupe_key="meta-901")

    fake_runtime = _FakeRuntimeRegistry()
    worker = StationWorker(
        station_id=21,
        worker_id="worker-1",
        runtime_registry=fake_runtime,
        fallback_uri="",
    )

    out = worker.process_once()
    assert out["source"] == "manual"
    assert len(fake_runtime.starts) == 1
    assert fake_runtime.starts[0]["station_id"] == 21
    assert fake_runtime.starts[0]["input_uri"] == "C:/music/My%20Song.mp3"
    assert fake_runtime.starts[0]["stream_title"] == "My Song"
    assert fake_runtime.starts[0]["stream_artist"] == ""
