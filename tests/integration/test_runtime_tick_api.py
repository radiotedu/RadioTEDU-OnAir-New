from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
import app.api.runtime as runtime_api
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

    def stop_station(self, station_id: int):
        return {"station_id": station_id, "running": False}

    def status(self, station_id: int):
        return {
            "station_id": station_id,
            "running": bool(self.starts),
            "branch_health": {"icecast": True, "local": True},
        }


def test_runtime_tick_starts_manual_track_playout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (id, title, artist, musicbrainz_recordingid, file_path) VALUES (?, ?, ?, ?, ?)",
        (88, "Song", "Artist", "", "C:/music/manual.mp3"),
    )
    conn.commit()
    QueueRepository(conn).enqueue(station_id=1, track_id=88, dedupe_key="tick-1")
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})
    SettingsRepository(conn).upsert_station(
        1,
        {"broadcast_autostart_enabled": "true"},
    )

    fake = _FakeRuntimeRegistry()
    monkeypatch.setattr(runtime_api, "runtime_registry", fake)

    client = TestClient(app)
    res = client.post("/api/runtime/1/tick", json={"fallback_uri": "C:/music/fallback.mp3"})
    assert res.status_code == 200
    assert res.json()["source"] == "manual"
    assert fake.starts == [
        {
            "station_id": 1,
            "input_uri": "C:/music/manual.mp3",
            "stream_title": "Song",
            "stream_artist": "Artist",
            "track_type": "music",
            "crossfade_seconds": 3.0,
        }
    ]
