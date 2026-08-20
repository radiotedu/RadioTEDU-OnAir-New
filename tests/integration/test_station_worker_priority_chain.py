from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.queue_repo import QueueRepository
from app.repositories.schedule_repo import ScheduleRepository


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
        self.starts.append((station_id, input_uri))
        return {"station_id": station_id, "running": True}

    def status(self, station_id: int):
        return {"station_id": int(station_id), "running": True}


def test_worker_follows_manual_ads_schedule_fallback_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (9, 'Priority Test')")
    cur.executemany(
        "INSERT INTO tracks "
        "(id, station_id, title, artist, track_type, musicbrainz_recordingid, file_path, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (501, 9, "ManualSong", "A", "music", "", "C:/music/manual.mp3", 1),
            (502, 9, "AdSong", "B", "ad", "", "C:/music/ad.mp3", 1),
            (503, 9, "ScheduleSong", "C", "music", "", "C:/music/schedule.mp3", 1),
        ],
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (9, 'hourly_ad_enabled', 'true') "
        "ON CONFLICT(station_id, key) DO UPDATE SET value='true'"
    )
    conn.commit()

    QueueRepository(conn).enqueue(station_id=9, track_id=501, dedupe_key="manual-1")
    AdBreakRepository(conn).enqueue(
        station_id=9,
        track_id=502,
        due_at="2000-01-01 00:00:00",
        priority=10,
    )
    ScheduleRepository(conn).enqueue(
        station_id=9,
        track_id=503,
        play_at="2000-01-01 00:00:00",
    )

    fake_runtime = _FakeRuntimeRegistry()
    worker = StationWorker(
        station_id=9,
        worker_id="worker-1",
        runtime_registry=fake_runtime,
        fallback_uri="C:/music/fallback.mp3",
    )

    out1 = worker.process_once()
    out2 = worker.process_once()
    out3 = worker.process_once()
    out4 = worker.process_once()

    assert out1["source"] == "manual"
    assert out2["source"] == "ads"
    assert out3["source"] == "schedule"
    assert out4["source"] == "fallback"
    assert fake_runtime.starts == [
        (9, "C:/music/manual.mp3"),
        (9, "C:/music/ad.mp3"),
        (9, "C:/music/schedule.mp3"),
        (9, "C:/music/fallback.mp3"),
    ]
