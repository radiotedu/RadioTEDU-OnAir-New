from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.queue_repo import QueueRepository
from app.repositories.settings_repo import SettingsRepository


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
                "station_id": int(station_id),
                "input_uri": str(input_uri),
                "stream_title": str(stream_title or ""),
                "stream_artist": str(stream_artist or ""),
                "track_type": str(track_type or "music"),
                "crossfade_seconds": float(crossfade_seconds),
            }
        )
        return {"station_id": station_id, "running": True}


def test_worker_starts_runtime_with_track_file_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (id, title, artist, musicbrainz_recordingid, file_path) VALUES (?, ?, ?, ?, ?)",
        (77, "Song", "Artist", "", "C:/music/song.mp3"),
    )
    conn.commit()
    QueueRepository(conn).enqueue(station_id=1, track_id=77, dedupe_key="m1")
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})

    fake_runtime = _FakeRuntimeRegistry()
    worker = StationWorker(
        station_id=1,
        worker_id="worker-1",
        runtime_registry=fake_runtime,
        fallback_uri="C:/music/fallback.mp3",
    )
    out = worker.process_once()

    assert out["source"] == "manual"
    assert fake_runtime.starts == [
        {
            "station_id": 1,
            "input_uri": "C:/music/song.mp3",
            "stream_title": "Song",
            "stream_artist": "Artist",
            "track_type": "music",
            "crossfade_seconds": 3.0,
        }
    ]
