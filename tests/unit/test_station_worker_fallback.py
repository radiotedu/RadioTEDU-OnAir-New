import json
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.settings_repo import SettingsRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.started: list[dict] = []
        self.running: dict[int, bool] = {}
        self.transition_active: dict[int, bool] = {}
        self.branch_health: dict[int, dict[str, bool]] = {}
        self.required_outputs: dict[int, dict[str, bool]] = {}
        self.active_input_uri: dict[int, str] = {}

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float | None = None,
        start_offset_seconds: float = 0.0,
    ):
        sid = int(station_id)
        self.started.append(
            {
                "station_id": sid,
                "input_uri": str(input_uri),
                "stream_title": str(stream_title or ""),
                "stream_artist": str(stream_artist or ""),
                "track_type": str(track_type or "music"),
                "crossfade_seconds": float(crossfade_seconds or 0.0),
                "start_offset_seconds": float(start_offset_seconds or 0.0),
            }
        )
        self.running[sid] = True
        return {"station_id": sid, "running": True}

    def status(self, station_id: int):
        sid = int(station_id)
        is_running = bool(self.running.get(sid, False))
        branch_health = self.branch_health.get(
            sid, {"icecast": False, "local": is_running}
        )
        required_outputs = self.required_outputs.get(
            sid, {"icecast": False, "local": True}
        )
        return {
            "station_id": sid,
            "running": is_running,
            "transition_active": bool(self.transition_active.get(sid, False)),
            "branch_health": branch_health,
            "required_outputs": required_outputs,
            "active_input_uri": self.active_input_uri.get(sid, ""),
        }


def _started_at_seconds_ago(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=int(seconds))
    ).strftime("%Y-%m-%d %H:%M:%S")


def test_worker_uses_fallback_when_no_other_source():
    worker = StationWorker(station_id=1)
    source = worker.decide_next_source(
        manual_count=0, ad_due=False, schedule_ready=False, fallback_ready=True
    )
    assert source == "fallback"


def test_startup_sound_does_not_duplicate_preserved_front_jingle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, track_type, file_path, is_active) "
        "VALUES (1, 'RadioTEDU Sweeper', 'jingle', 'C:/jingles/radiotedu.mp3', 1)"
    )
    jingle_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items "
        "(station_id, track_id, position, status, dedupe_key) "
        "VALUES (1, ?, 1, 'pending', 'jingle:preserved-front')",
        (jingle_track_id,),
    )
    SettingsRepository(conn).upsert_station(
        1,
        {
            "_startup_sound_pending": "true",
            "startup_sound_enabled": "true",
            "startup_sound_mode": "specific",
            "startup_sound_track_id": str(jingle_track_id),
        },
    )
    conn.commit()

    worker = StationWorker(station_id=1, runtime_registry=_FakeRuntimeRegistry())

    assert worker._maybe_insert_startup_sound() is False
    rows = conn.execute(
        "SELECT track_id, status FROM queue_items "
        "WHERE station_id=1 AND status IN ('pending','playing') "
        "ORDER BY position,id"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (jingle_track_id, "pending")
    ]
    settings = SettingsRepository(conn).get_station(1)
    assert settings["_startup_sound_pending"] == "false"


def test_worker_process_once_autofills_empty_queue_and_starts_first_track(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Busy Song', 'Artist B', 'music', 'C:/music/busy.mp3', 1, 4, 0)"
    )
    busy_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    fresh_track_id = int(cur.lastrowid)
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/fresh.mp3"
    assert int(result["item_id"]) > 0
    assert runtime.started == [
        {
            "station_id": 1,
            "input_uri": "C:/music/fresh.mp3",
            "stream_title": "Fresh Song",
            "stream_artist": "Artist A",
            "track_type": "music",
            "crossfade_seconds": 3.0,
            "start_offset_seconds": 0.0,
        }
    ]
    rows = conn.cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (fresh_track_id, "playing"),
        (busy_track_id, "pending"),
    ]

    second = worker.process_once()
    assert second == {"source": "playing", "reason": "track_in_progress"}
    assert len(runtime.started) == 1
    rows = conn.cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (fresh_track_id, "playing"),
        (busy_track_id, "pending"),
    ]

    runtime.running[1] = False
    third = worker.process_once()
    assert third["source"] == "manual"
    assert third["input_uri"] == "C:/music/busy.mp3"
    assert len(runtime.started) == 2
    rows = conn.cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (fresh_track_id, "done"),
        (busy_track_id, "playing"),
    ]
    counts = conn.cursor().execute(
        "SELECT id, play_count FROM tracks WHERE id IN (?, ?) ORDER BY id ASC",
        (fresh_track_id, busy_track_id),
    ).fetchall()
    assert [(int(row["id"]), int(row["play_count"])) for row in counts] == [
        (busy_track_id, 4),
        (fresh_track_id, 1),
    ]


def test_worker_process_once_stays_idle_when_library_has_no_autoplay_candidates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result == {"source": "none"}
    assert runtime.started == []


def test_worker_autofill_is_strictly_isolated_to_its_station(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (2, 'Lo-Fi')")
    cur.execute(
        "INSERT INTO tracks "
        "(station_id, title, artist, genre, track_type, duration, file_path, "
        "is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Classical Leak', 'Orchestra', 'Classical', 'music', "
        "240, 'E:/classical/leak.flac', 1, 0, 0)"
    )
    classical_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks "
        "(station_id, title, artist, genre, track_type, duration, file_path, "
        "is_active, play_count, exclude_from_autoplay) "
        "VALUES (2, 'Lo-Fi Only', 'Beatmaker', 'Lo-Fi', 'music', "
        "180, 'E:/lofi/only.mp3', 1, 12, 0)"
    )
    lofi_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items "
        "(station_id, track_id, position, status, started_at, dedupe_key) "
        "VALUES (2, ?, 1, 'playing', CURRENT_TIMESTAMP, 'legacy-playing')",
        (classical_id,),
    )
    cur.execute(
        "INSERT INTO queue_items "
        "(station_id, track_id, position, status, dedupe_key) "
        "VALUES (2, ?, 2, 'pending', 'legacy-pending')",
        (classical_id,),
    )
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    runtime.running[2] = True
    runtime.active_input_uri[2] = "E:/classical/leak.flac"
    worker = StationWorker(station_id=2, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "E:/lofi/only.mp3"
    assert runtime.started[-1]["input_uri"] == "E:/lofi/only.mp3"
    active_rows = conn.execute(
        "SELECT q.track_id, t.station_id AS track_station, q.status "
        "FROM queue_items q JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=2 AND q.status IN ('pending','playing')"
    ).fetchall()
    assert active_rows
    assert {int(row["track_station"]) for row in active_rows} == {2}
    assert any(int(row["track_id"]) == lofi_id for row in active_rows)


def test_worker_recovers_when_live_runtime_uri_does_not_match_playing_item(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (2, 'Lo-Fi')")
    cur.execute(
        "INSERT INTO tracks "
        "(station_id, title, artist, track_type, duration, file_path, "
        "is_active, play_count, exclude_from_autoplay) "
        "VALUES (2, 'Expected Lo-Fi', 'Beatmaker', 'music', 180, "
        "'E:/lofi/expected.mp3', 1, 0, 0)"
    )
    track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items "
        "(station_id, track_id, position, status, started_at, dedupe_key) "
        "VALUES (2, ?, 1, 'playing', CURRENT_TIMESTAMP, 'expected')",
        (track_id,),
    )
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    runtime.running[2] = True
    runtime.active_input_uri[2] = "E:/classical/wrong.flac"
    worker = StationWorker(station_id=2, runtime_registry=runtime)

    result = worker.process_once()

    assert result == {"source": "playing", "reason": "track_in_progress"}
    assert runtime.started[-1]["input_uri"] == "E:/lofi/expected.mp3"


def test_worker_process_once_does_not_sync_preload_upcoming_ai_announcements(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    def _unexpected_sync_preload():
        raise AssertionError("worker tick should not synchronously preload AI announcements")

    monkeypatch.setattr(worker, "_preload_upcoming_ai_announcements", _unexpected_sync_preload)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/fresh.mp3"
    assert runtime.started[0]["track_type"] == "music"


def test_worker_process_once_queues_ai_intro_before_music_track(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    music_track_id = int(cur.lastrowid)
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "0",
        },
    )

    intro_path = tmp_path / "ai_intro.wav"
    intro_path.write_bytes(b"R" * 4096)

    class _FakeAIHost:
        def generate_station_id_announcement(self, **kwargs):
            return None

        def generate_track_intro_announcement(self, **kwargs):
            return SimpleNamespace(
                audio_path=str(intro_path),
                duration_seconds=4.0,
                title="AI Intro - Fresh Song",
                artist="AI Host",
            )

    monkeypatch.setattr("app.services.ai_host.get_ai_host", lambda: _FakeAIHost())
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _FakeAIHost())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == str(intro_path)
    assert runtime.started[0]["track_type"] == "announcement"
    rows = conn.cursor().execute(
        "SELECT q.status, COALESCE(t.track_type, 'music') AS track_type, COALESCE(t.title, '') AS title "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [(str(row["status"]), str(row["track_type"]), str(row["title"])) for row in rows] == [
        ("playing", "announcement", "AI Intro - Fresh Song"),
        ("pending", "music", "Fresh Song"),
    ]

    runtime.running[1] = False
    second = worker.process_once()

    # A dead runtime is recovered once with the same item before the worker is
    # allowed to advance, preventing an encoder restart from skipping audio.
    assert second["source"] == "playing"
    assert second["reason"] == "track_in_progress"

    runtime.running[1] = False
    third = worker.process_once()
    assert third["source"] == "manual"
    assert third["input_uri"] == "C:/music/fresh.mp3"
    rows = conn.cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    announcement_track_id = int(rows[0]["track_id"])
    assert [(int(row["track_id"]), str(row["status"]), str(row["track_type"])) for row in rows] == [
        (announcement_track_id, "failed", "announcement"),
        (music_track_id, "playing", "music"),
    ]


def test_worker_process_once_prepares_intro_for_music_behind_pending_jingle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Station Sweeper', 'TEDU', 'jingle', 7, 'C:/audio/sweeper.mp3', 1, 0, 0)"
    )
    jingle_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    music_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) VALUES (1, ?, 1, 'pending', 'jingle:1')",
        (jingle_track_id,),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) VALUES (1, ?, 2, 'pending', 'music:1')",
        (music_track_id,),
    )
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "0",
        },
    )

    intro_path = tmp_path / "queued_intro.wav"
    intro_path.write_bytes(b"R" * 4096)

    class _FakeAIHost:
        def generate_station_id_announcement(self, **kwargs):
            return None

        def generate_track_intro_announcement(self, **kwargs):
            return SimpleNamespace(
                audio_path=str(intro_path),
                duration_seconds=4.0,
                title="AI Intro - Fresh Song",
                artist="AI Host",
            )

    monkeypatch.setattr("app.services.ai_host.get_ai_host", lambda: _FakeAIHost())
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _FakeAIHost())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == str(intro_path)
    rows = conn.cursor().execute(
        "SELECT q.status, COALESCE(t.track_type, 'music') AS track_type, COALESCE(t.title, '') AS title "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [(str(row["status"]), str(row["track_type"]), str(row["title"])) for row in rows] == [
        ("playing", "announcement", "AI Intro - Fresh Song"),
        ("pending", "music", "Fresh Song"),
    ]


def test_worker_process_once_prioritizes_track_intro_before_station_id(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    music_track_id = int(cur.lastrowid)
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "1800",
        },
    )

    station_id_path = tmp_path / "station_id.wav"
    station_id_path.write_bytes(b"S" * 4096)
    intro_path = tmp_path / "track_intro.wav"
    intro_path.write_bytes(b"R" * 4096)

    class _FakeAIHost:
        def generate_station_id_announcement(self, **kwargs):
            return SimpleNamespace(
                audio_path=str(station_id_path),
                duration_seconds=3.0,
                title="AI Station ID",
                artist="AI Host",
            )

        def generate_track_intro_announcement(self, **kwargs):
            return SimpleNamespace(
                audio_path=str(intro_path),
                duration_seconds=4.0,
                title="AI Intro - Fresh Song",
                artist="AI Host",
            )

    monkeypatch.setattr("app.services.ai_host.get_ai_host", lambda: _FakeAIHost())
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _FakeAIHost())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == str(intro_path)
    rows = conn.cursor().execute(
        "SELECT q.status, COALESCE(t.track_type, 'music') AS track_type, COALESCE(t.title, '') AS title "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [(str(row["status"]), str(row["track_type"]), str(row["title"])) for row in rows] == [
        ("playing", "announcement", "AI Intro - Fresh Song"),
        ("pending", "music", "Fresh Song"),
    ]
    assert all(str(row["title"]) != "AI Station ID" for row in rows)
    assert music_track_id > 0


def test_worker_process_once_skips_cold_fast_ai_inline_generation_on_first_track(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "0",
        },
    )

    class _ColdFastAI:
        def get_load_status(self):
            return {"llm_loaded": False}

        def generate_station_id_announcement(self, **kwargs):
            raise AssertionError("cold fast AI should not be called inline for station IDs")

        def generate_track_intro_announcement(self, **kwargs):
            raise AssertionError("cold fast AI should not be called inline for track intros")

    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _ColdFastAI())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/fresh.mp3"
    rows = conn.cursor().execute(
        "SELECT q.status, COALESCE(t.track_type, 'music') AS track_type, COALESCE(t.title, '') AS title "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [(str(row["status"]), str(row["track_type"]), str(row["title"])) for row in rows] == [
        ("playing", "music", "Fresh Song"),
    ]


def test_worker_process_once_skips_inline_generation_for_omnivoice_provider(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "0",
            "ai_tts_provider": "omnivoice",
        },
    )

    class _WarmAI:
        def get_load_status(self, settings=None):
            return {"llm_loaded": True, "tts_provider": "omnivoice"}

        def generate_station_id_announcement(self, **kwargs):
            raise AssertionError("omnivoice should not run inline in the worker")

        def generate_track_intro_announcement(self, **kwargs):
            raise AssertionError("omnivoice should not run inline in the worker")

    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _WarmAI())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/fresh.mp3"


def test_worker_process_once_keeps_ai_prefetch_running_when_enabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 32, 'C:/music/fresh.mp3', 1, 0, 0)"
    )
    conn.commit()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "ai_host_enabled": "true",
            "ai_station_id_interval": "0",
            "ai_tts_provider": "omnivoice",
        },
    )

    calls: list[int] = []

    class _FakePrefetch:
        def ensure_running(self, station_id: int):
            calls.append(int(station_id))
            return {"station_id": int(station_id), "running": True, "thread_alive": True}

    class _WarmAI:
        def get_load_status(self, settings=None):
            return {"llm_loaded": True, "tts_provider": "omnivoice"}

        def generate_station_id_announcement(self, **kwargs):
            raise AssertionError("omnivoice should not run inline in the worker")

        def generate_track_intro_announcement(self, **kwargs):
            raise AssertionError("omnivoice should not run inline in the worker")

    monkeypatch.setattr("app.services.ai_prefetch.get_ai_prefetch", lambda: _FakePrefetch())
    monkeypatch.setattr("app.services.ai_host_fast.get_ai_host_fast", lambda: _WarmAI())

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/fresh.mp3"
    assert calls == [1]


def test_worker_prefers_matching_prefetched_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)

    edge_audio = tmp_path / "edge.wav"
    omni_audio = tmp_path / "omni.wav"
    edge_audio.write_bytes(b"E" * 64)
    omni_audio.write_bytes(b"O" * 64)
    (tmp_path / "announcement_edge.json").write_text(
        json.dumps(
            {
                "cache_key": "edge",
                "station_id": 1,
                "station_name": "Radio TEDU",
                "announcement_type": "track_intro",
                "title": "AI Intro - Fresh Song",
                "artist": "AI Host",
                "text": "Edge version",
                "audio_path": str(edge_audio),
                "duration_seconds": 3.0,
                "llm_provider": "local-qwen",
                "tts_provider": "edge-tts",
                "generated_at": "2026-04-12T00:00:00+00:00",
                "dedupe_key": "ai-track-intro:77",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "announcement_omni.json").write_text(
        json.dumps(
            {
                "cache_key": "omni",
                "station_id": 1,
                "station_name": "Radio TEDU",
                "announcement_type": "track_intro",
                "title": "AI Intro - Fresh Song",
                "artist": "AI Host",
                "text": "Omni version",
                "audio_path": str(omni_audio),
                "duration_seconds": 3.0,
                "llm_provider": "local-qwen",
                "tts_provider": "omnivoice",
                "generated_at": "2026-04-12T00:00:00+00:00",
                "dedupe_key": "ai-track-intro:77",
            }
        ),
        encoding="utf-8",
    )

    worker = StationWorker(station_id=1)
    announcement = worker._find_prefetched_announcement(
        "ai-track-intro:77",
        settings={"ai_tts_provider": "omnivoice"},
    )

    assert announcement is not None
    assert announcement.tts_provider == "omnivoice"
    assert announcement.audio_path == str(omni_audio)


def test_worker_autofill_backfills_zero_duration_track_before_queueing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    audio_file = tmp_path / "fresh-song.mp3"
    audio_file.write_bytes(b"ID3")

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 0, ?, 1, 0, 0)",
        (str(audio_file),),
    )
    track_id = int(cur.lastrowid)
    conn.commit()

    monkeypatch.setattr(
        "app.engine.station_worker.resolve_runtime_media_path",
        lambda raw: str(audio_file) if str(raw) == str(audio_file) else str(raw),
    )
    monkeypatch.setattr(
        "app.audio.audio_processing.probe_duration",
        lambda file_path, **_kwargs: 321.5 if str(file_path) == str(audio_file) else 0.0,
    )

    worker = StationWorker(station_id=1)
    selected = worker._select_random_music_track(set())

    assert selected == {"track_id": track_id, "duration": 321.5}
    row = conn.cursor().execute("SELECT duration FROM tracks WHERE id=?", (track_id,)).fetchone()
    assert row is not None
    assert float(row["duration"] or 0.0) == 321.5


def test_worker_shuffle_seed_is_reproducible_within_least_played_tier(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {"autoplay_shuffle_seed": "operator-seed-2026"},
    )
    track_ids = []
    for index in range(8):
        cur = conn.execute(
            "INSERT INTO tracks "
            "(station_id, title, artist, track_type, duration, file_path, "
            "is_active, play_count, exclude_from_autoplay) "
            "VALUES (1, ?, 'Artist', 'music', 100, ?, 1, 0, 0)",
            (f"Track {index}", str(tmp_path / f"track-{index}.mp3")),
        )
        track_ids.append(int(cur.lastrowid))
    conn.commit()

    expected = min(
        track_ids,
        key=lambda track_id: hashlib.sha256(
            f"operator-seed-2026:{track_id}".encode("utf-8")
        ).digest(),
    )
    worker = StationWorker(station_id=1)

    first = worker._select_random_music_track(set())
    second = worker._select_random_music_track(set())

    assert first == second
    assert first == {"track_id": expected, "duration": 100.0}


def test_worker_process_once_prerolls_next_music_track_inside_crossfade_window(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Current Song', 'Artist A', 'music', 30, 'C:/music/current.mp3', 1, 0, 0)"
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Next Song', 'Artist B', 'music', 40, 'C:/music/next.mp3', 1, 4, 0)"
    )
    next_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', ?)",
        (1, current_track_id, _started_at_seconds_ago(28)),
    )
    current_item_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, 2, 'pending')",
        (1, next_track_id),
    )
    next_item_id = int(cur.lastrowid)
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    runtime.running[1] = True
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/next.mp3"
    assert int(result["item_id"]) == next_item_id
    assert runtime.started == [
        {
            "station_id": 1,
            "input_uri": "C:/music/next.mp3",
            "stream_title": "Next Song",
            "stream_artist": "Artist B",
            "track_type": "music",
            "crossfade_seconds": 3.0,
            "start_offset_seconds": 0.0,
        }
    ]
    rows = conn.cursor().execute(
        "SELECT id, track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["id"]), int(row["track_id"]), str(row["status"])) for row in rows] == [
        (current_item_id, current_track_id, "done"),
        (next_item_id, next_track_id, "playing"),
    ]
    counts = conn.cursor().execute(
        "SELECT id, play_count FROM tracks WHERE id IN (?, ?) ORDER BY id ASC",
        (current_track_id, next_track_id),
    ).fetchall()
    assert [(int(row["id"]), int(row["play_count"])) for row in counts] == [
        (current_track_id, 1),
        (next_track_id, 4),
    ]


def test_worker_process_once_refills_pending_autoplay_when_only_current_music_is_playing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Current Song', 'Artist A', 'music', 30, 'C:/music/current.mp3', 1, 0, 0)"
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Library Song', 'Artist B', 'music', 35, 'C:/music/library.mp3', 1, 2, 0)"
    )
    next_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', ?)",
        (1, current_track_id, _started_at_seconds_ago(28)),
    )
    current_item_id = int(cur.lastrowid)
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    runtime.running[1] = True
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == "C:/music/library.mp3"
    rows = conn.cursor().execute(
        "SELECT id, track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (current_track_id, "done"),
        (next_track_id, "playing"),
    ]
    assert int(rows[0]["id"]) == current_item_id


def test_worker_process_once_remaps_stale_library_path_to_current_uploads_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    audio_file = tmp_path / "uploads" / "station-1" / "music" / "legacy-song.mp3"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"ID3")
    stale_file_path = (
        "E:/liquidsoap-2.4.2-win64/radio-automation/cleanroom/backend/"
        "data/uploads/station-1/music/legacy-song.mp3"
    )
    conn.cursor().execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Legacy Song', 'Artist A', 'music', ?, 1, 0, 0)",
        (stale_file_path,),
    )
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result["source"] == "manual"
    assert result["input_uri"] == str(audio_file.resolve())
    assert runtime.started[-1]["input_uri"] == str(audio_file.resolve())


def test_worker_recent_track_ids_only_considers_last_queue_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    history_track_ids = list(range(1, 61)) + [80, 81, 82, 80, 80, 80, 80, 80, 83, 84, 85]
    for position, track_id in enumerate(history_track_ids, start=1):
        cur.execute(
            "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, ?, 'done')",
            (1, track_id, position),
        )
    conn.commit()

    worker = StationWorker(station_id=1)

    assert worker._recent_track_ids(limit=30) == set(history_track_ids[-30:])


def test_worker_process_once_keeps_current_track_when_local_branch_is_still_healthy(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Current Song', 'Artist A', 'music', 30, 'C:/music/current.mp3', 1, 0, 0)"
    )
    current_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Next Song', 'Artist B', 'music', 40, 'C:/music/next.mp3', 1, 0, 0)"
    )
    next_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', ?)",
        (1, current_track_id, _started_at_seconds_ago(5)),
    )
    current_item_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (?, ?, 2, 'pending')",
        (1, next_track_id),
    )
    next_item_id = int(cur.lastrowid)
    conn.commit()

    runtime = _FakeRuntimeRegistry()
    runtime.running[1] = False
    runtime.branch_health[1] = {"icecast": False, "local": True}
    runtime.required_outputs[1] = {"icecast": True, "local": True}
    worker = StationWorker(station_id=1, runtime_registry=runtime)

    result = worker.process_once()

    assert result == {"source": "playing", "reason": "track_in_progress"}
    assert runtime.started == []
    rows = conn.cursor().execute(
        "SELECT id, track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["id"]), int(row["track_id"]), str(row["status"])) for row in rows] == [
        (current_item_id, current_track_id, "playing"),
        (next_item_id, next_track_id, "pending"),
    ]
