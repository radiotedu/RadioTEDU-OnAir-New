from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.queue_repo import QueueRepository
from app.repositories.schedule_repo import ScheduleRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.starts = []
        self.active_input_uri = {}
        self.finished_inputs = set()

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
        self.active_input_uri[int(station_id)] = str(input_uri)
        self.finished_inputs.discard(str(input_uri))
        return {"station_id": station_id, "running": True}

    def status(self, station_id: int):
        sid = int(station_id)
        uri = self.active_input_uri.get(sid, "")
        eof = uri in self.finished_inputs
        return {
            "station_id": sid,
            "running": True,
            "program_running": not eof,
            "producer_eof": eof,
            "active_input_uri": uri,
        }


def test_worker_honors_due_ads_and_waits_for_active_sources(tmp_path, monkeypatch):
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
    assert out1["source"] == "ads"
    while_ad_plays = worker.process_once()
    assert while_ad_plays["reason"] == "ad_in_progress"
    fake_runtime.finished_inputs.add("C:/music/ad.mp3")

    out2 = worker.process_once()
    assert out2["source"] == "manual"
    while_song_plays = worker.process_once()

    # Due ads take precedence at a free boundary. Active songs and ads retain
    # their ownership until their exact producer reaches a clean EOF.
    assert while_song_plays == {
        "source": "playing",
        "reason": "track_in_progress",
    }
    fake_runtime.finished_inputs.add("C:/music/manual.mp3")

    out3 = worker.process_once()
    assert out3["source"] == "schedule"
    fake_runtime.finished_inputs.add("C:/music/schedule.mp3")

    out4 = worker.process_once()
    assert out4["source"] == "fallback"
    assert fake_runtime.starts == [
        (9, "C:/music/ad.mp3"),
        (9, "C:/music/manual.mp3"),
        (9, "C:/music/schedule.mp3"),
        (9, "C:/music/fallback.mp3"),
    ]
