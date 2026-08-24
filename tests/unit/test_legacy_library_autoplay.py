from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.engine.broadcast_queue_autofill import (
    _count_music_since_last_jingle as count_autofill_music_since_jingle,
    ensure_broadcast_queue_filled,
)
from app.engine.station_worker import StationWorker
from app.main import app
from app.repositories.queue_repo import QueueRepository
from app.repositories.settings_repo import SettingsRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.running: dict[int, bool] = {}
        self.started: list[dict] = []
        self.branch_health: dict[int, dict[str, bool]] = {}
        self.required_outputs: dict[int, dict[str, bool]] = {}

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float = 0.0,
    ):
        sid = int(station_id)
        self.started.append(
            {
                "station_id": sid,
                "input_uri": str(input_uri),
                "stream_title": str(stream_title or ""),
                "stream_artist": str(stream_artist or ""),
                "track_type": str(track_type or "music"),
                "crossfade_seconds": float(crossfade_seconds),
            }
        )
        self.running[sid] = True
        return self.status(sid)

    def stop_station(self, station_id: int):
        sid = int(station_id)
        self.running[sid] = False
        return self.status(sid)
    def status(self, station_id: int):
        sid = int(station_id)
        running = bool(self.running.get(sid, False))
        branch_health = self.branch_health.get(
            sid, {"icecast": running, "local": False}
        )
        required_outputs = self.required_outputs.get(
            sid, {"icecast": True, "local": False}
        )
        return {
            "station_id": sid,
            "running": running,
            "branch_health": branch_health,
            "required_outputs": required_outputs,
        }


def test_sweeper_cadence_uses_playback_time_when_jingle_was_inserted_late(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    track_ids = {}
    for title, track_type in (
        ("Song 1", "music"),
        ("Song 2", "music"),
        ("Song 3", "music"),
        ("Late inserted ID", "jingle"),
    ):
        cursor = conn.execute(
            "INSERT INTO tracks "
            "(station_id, title, track_type, file_path, is_active) "
            "VALUES (1, ?, ?, ?, 1)",
            (title, track_type, str(tmp_path / f"{title}.flac")),
        )
        track_ids[title] = int(cursor.lastrowid)

    # The three prefilled songs receive lower queue ids. The station ID is
    # inserted later (higher id) but plays first, which is the production shape
    # that exposed the enqueue-order bug.
    for position, title, started in (
        (1, "Song 1", "2026-08-24 01:01:00"),
        (2, "Song 2", "2026-08-24 01:02:00"),
        (3, "Song 3", "2026-08-24 01:03:00"),
        (4, "Late inserted ID", "2026-08-24 01:00:00"),
    ):
        conn.execute(
            "INSERT INTO queue_items "
            "(station_id, track_id, position, status, started_at, finished_at) "
            "VALUES (1, ?, ?, 'done', ?, ?)",
            (track_ids[title], position, started, started),
        )
    conn.commit()

    worker = StationWorker(1)
    try:
        assert worker._count_music_since_last_jingle() == 3
        assert count_autofill_music_since_jingle(conn, 1) == 3
    finally:
        worker.conn.close()
        conn.close()


def _stub_worker_loop_start(monkeypatch):
    def _fake_loop_start(*, station_id, fallback_uri="", interval_sec=1.0):
        return {
            "station_id": int(station_id),
            "running": True,
            "interval_sec": float(interval_sec),
            "fallback_uri": str(fallback_uri),
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }

    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", _fake_loop_start)


def test_liquidsoap_status_does_not_autoplay_library_when_queue_is_empty(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()

    a1 = tmp_path / "song-a.mp3"
    a2 = tmp_path / "song-b.mp3"
    a1.write_bytes(b"ID3")
    a2.write_bytes(b"ID3")
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Song A", "Artist A", "music", str(a1)),
    )
    t1 = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Song B", "Artist B", "music", str(a2)),
    )
    t2 = int(cur.lastrowid)
    conn.commit()
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})

    c = TestClient(app)

    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("alive") is False
    assert payload.get("current_track") is None
    assert payload.get("status") == "inactive"
    assert fake_runtime.started == []


def test_liquidsoap_status_stays_inactive_when_library_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)
    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("alive") is False
    assert payload.get("current_track") is None
    assert payload.get("status") == "inactive"
    assert fake_runtime.started == []


def test_liquidsoap_status_reports_elapsed_for_library_fallback_track(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    fake_runtime.running[1] = True
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()

    audio = tmp_path / "fallback-song.mp3"
    audio.write_bytes(b"ID3")
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, duration, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (1, "Fallback Song", "Fallback Artist", "music", str(audio), 120.0),
    )
    track_id = int(cur.lastrowid)
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    cur.execute(
        "INSERT INTO playout_state (station_id, current_source, current_item_id, started_at) VALUES (?, ?, ?, ?)",
        (1, "library_fallback", track_id, started_at),
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200

    payload = res.json()
    current_track = payload.get("current_track") or {}
    assert int(current_track.get("id") or 0) == track_id
    assert float(payload.get("elapsed") or 0) >= 4.0
    assert 110.0 <= float(payload.get("remaining") or 0) <= 116.0


def test_liquidsoap_status_reports_alive_when_local_branch_is_healthy(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    fake_runtime.running[1] = False
    fake_runtime.branch_health[1] = {"icecast": False, "local": True}
    fake_runtime.required_outputs[1] = {"icecast": True, "local": True}
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()

    audio = tmp_path / "local-monitor-song.mp3"
    audio.write_bytes(b"ID3")
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, duration, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (1, "Local Monitor Song", "Fallback Artist", "music", str(audio), 180.0),
    )
    track_id = int(cur.lastrowid)
    queue = QueueRepository(conn)
    item_id = queue.enqueue(1, track_id, "local-monitor-song")
    queue.mark_playing(item_id)

    cur.execute(
        "INSERT OR REPLACE INTO station_outputs (station_id, local_output_enabled, output_device_id, icecast_enabled, icecast_host, icecast_port, icecast_mount, icecast_user, icecast_password, output_gain_db) "
        "VALUES (?, 1, '', 1, '127.0.0.1', 8000, '/stream', 'source', 'hackme', 0)",
        (1,),
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200

    payload = res.json()
    current_track = payload.get("current_track") or {}
    assert payload.get("alive") is True
    assert payload.get("status") == "active"
    assert payload.get("local_monitor_active") is True
    assert int(current_track.get("id") or 0) == track_id


def test_liquidsoap_status_does_not_advance_queue_after_runtime_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()

    first_audio = tmp_path / "queue-a.mp3"
    second_audio = tmp_path / "queue-b.mp3"
    first_audio.write_bytes(b"ID3")
    second_audio.write_bytes(b"ID3")
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Queue A", "Artist A", "music", str(first_audio)),
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Queue B", "Artist B", "music", str(second_audio)),
    )
    second_track_id = int(cur.lastrowid)
    conn.commit()
    SettingsRepository(conn).upsert_system({"default_crossfade_seconds": "3.0"})

    queue = QueueRepository(conn)
    first_item_id = queue.enqueue(1, first_track_id, "queue-a")
    second_item_id = queue.enqueue(1, second_track_id, "queue-b")
    queue.mark_playing(first_item_id)
    fake_runtime.running[1] = False

    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200

    payload = res.json()
    current_track = payload.get("current_track") or {}
    assert int(current_track.get("id") or 0) == first_track_id
    assert payload.get("alive") is False
    assert fake_runtime.started == []

    check_conn = get_connection()
    rows = check_conn.cursor().execute(
        "SELECT id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC"
    ).fetchall()
    assert [(int(row["id"]), str(row["status"])) for row in rows] == [
        (first_item_id, "playing"),
        (second_item_id, "pending"),
    ]


def test_liquidsoap_status_does_not_create_playout_state_when_queue_has_track(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()

    audio = tmp_path / "queue-song.mp3"
    audio.write_bytes(b"ID3")
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (1, "Queue Song", "Artist", "music", str(audio)),
    )
    track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', CURRENT_TIMESTAMP)",
        (1, track_id),
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200

    row = get_connection().cursor().execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=1"
    ).fetchone()
    assert row is None


def test_liquidsoap_status_does_not_mutate_stale_library_state_on_read(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    fake_runtime = _FakeRuntimeRegistry()
    monkeypatch.setattr("app.api.runtime.runtime_registry", fake_runtime, raising=False)
    _stub_worker_loop_start(monkeypatch)

    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO playout_state (station_id, current_source, current_item_id, started_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (1, "library_fallback", 999999),
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/status", params={"station_id": 1})
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("current_track") is None

    row = get_connection().cursor().execute(
        "SELECT current_source, current_item_id FROM playout_state WHERE station_id=1"
    ).fetchone()
    assert row is not None
    assert str(row["current_source"]) == "library_fallback"
    assert int(row["current_item_id"]) == 999999


def test_liquidsoap_next_autofills_queue_with_least_played_track_when_empty(
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
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Blocked Song', 'Artist C', 'music', 'C:/music/blocked.mp3', 1, 0, 1)"
    )
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/next", params={"station_id": 1})
    assert res.status_code == 200
    assert res.text == "C:/music/fresh.mp3"

    rows = get_connection().cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows[:2]] == [
        (fresh_track_id, "playing"),
        (busy_track_id, "pending"),
    ]


def test_liquidsoap_next_inserts_sweeper_between_music_tracks_when_enabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "1",
            "sweeper_mode": "random",
        },
    )
    cur = conn.cursor()

    first_song = tmp_path / "first-song.mp3"
    second_song = tmp_path / "second-song.mp3"
    sweeper = tmp_path / "station-sweeper.mp3"
    first_song.write_bytes(b"ID3")
    second_song.write_bytes(b"ID3")
    sweeper.write_bytes(b"ID3")

    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
        (1, "First Song", "Artist A", "music", str(first_song)),
    )
    first_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, 1, 1, 0)",
        (1, "Second Song", "Artist B", "music", str(second_song)),
    )
    second_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
        (1, "Station Sweeper", "Voice", "jingle", str(sweeper)),
    )
    sweeper_track_id = int(cur.lastrowid)
    conn.commit()

    c = TestClient(app)
    res = c.get("/api/liquidsoap/next", params={"station_id": 1})
    assert res.status_code == 200
    assert res.text == str(first_song)

    rows = get_connection().cursor().execute(
        "SELECT q.track_id, q.status, COALESCE(t.track_type, 'music') AS track_type "
        "FROM queue_items q "
        "LEFT JOIN tracks t ON t.id = q.track_id "
        "WHERE q.station_id=1 ORDER BY q.position ASC, q.id ASC"
    ).fetchall()
    assert [
        (int(row["track_id"]), str(row["status"]), str(row["track_type"]))
        for row in rows[:3]
    ] == [
        (first_track_id, "playing", "music"),
        (sweeper_track_id, "pending", "jingle"),
        (second_track_id, "pending", "music"),
    ]

    queue_items = (c.get("/api/queue", params={"station_id": 1}).json().get("items") or [])
    active_items = [item for item in queue_items if not item.get("is_played")]
    assert [str(item["track_type"]) for item in active_items[:3]] == [
        "music",
        "jingle",
        "music",
    ]


def test_autofill_places_global_ad_immediately_after_three_song_jingle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "3",
            "sweeper_interval_unit": "tracks",
            "sweeper_mode": "ordered",
        },
    )
    track_ids = {}
    for name, track_type, play_count in (
        ("Song 1", "music", 0),
        ("Song 2", "music", 0),
        ("Song 3", "music", 0),
        ("Song 4", "music", 1),
        ("Genre Jingle", "jingle", 0),
        ("Global Ad", "ad", 0),
    ):
        cursor = conn.execute(
            "INSERT INTO tracks "
            "(station_id, title, track_type, file_path, is_active, play_count) "
            "VALUES (1, ?, ?, ?, 1, ?)",
            (name, track_type, str(tmp_path / f"{name}.flac"), play_count),
        )
        track_ids[name] = int(cursor.lastrowid)
    conn.commit()

    rows = ensure_broadcast_queue_filled(conn, station_id=1, refill_size=4)
    queue_types = [str(row["track_type"]) for row in rows]
    conn.close()

    assert queue_types == ["music", "music", "music", "jingle", "ad", "music"]


def test_broadcast_queue_autofill_avoids_immediate_sweeper_repeat_when_alternative_exists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "1",
            "sweeper_mode": "ordered",
        },
    )
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Next Song', 'Artist', 'music', 'C:/music/next-song.mp3', 1, 0, 0)"
    )
    music_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count) "
        "VALUES (1, 'Sweeper A', 'Voice', 'jingle', 'C:/jingles/a.mp3', 1, 0)"
    )
    sweeper_a_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count) "
        "VALUES (1, 'Sweeper B', 'Voice', 'jingle', 'C:/jingles/b.mp3', 1, 5)"
    )
    sweeper_b_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at, finished_at) "
        "VALUES (?, ?, 1, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (1, sweeper_a_id),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at, finished_at) "
        "VALUES (?, ?, 2, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (1, music_track_id),
    )
    conn.commit()

    rows = ensure_broadcast_queue_filled(conn, station_id=1, refill_size=1)
    assert [(int(row["track_id"]), str(row["status"])) for row in rows[:2]] == [
        (sweeper_b_id, "pending"),
        (music_track_id, "pending"),
    ]


def test_broadcast_queue_autofill_uses_playback_recency_not_queue_position_for_sweeper_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "1",
            "sweeper_mode": "ordered",
        },
    )
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Next Song', 'Artist', 'music', 'C:/music/next-song.mp3', 1, 0, 0)"
    )
    music_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count) "
        "VALUES (1, 'Recent Sweeper', 'Voice', 'jingle', 'C:/jingles/recent.mp3', 1, 0)"
    )
    recent_sweeper_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count) "
        "VALUES (1, 'Older Sweeper', 'Voice', 'jingle', 'C:/jingles/older.mp3', 1, 5)"
    )
    older_sweeper_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at, finished_at) "
        "VALUES (?, ?, 50, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (1, older_sweeper_id),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at, finished_at) "
        "VALUES (?, ?, 1, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (1, recent_sweeper_id),
    )
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at, finished_at) "
        "VALUES (?, ?, 2, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (1, music_track_id),
    )
    conn.commit()

    rows = ensure_broadcast_queue_filled(conn, station_id=1, refill_size=1)
    assert [(int(row["track_id"]), str(row["status"])) for row in rows[:2]] == [
        (older_sweeper_id, "pending"),
        (music_track_id, "pending"),
    ]


def test_liquidsoap_played_updates_track_play_count_and_last_played_at(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (1, 'Played Song', 'Artist P', 'music', 'C:/music/played.mp3', 1, 2, 0)"
    )
    track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status, started_at) VALUES (?, ?, 1, 'playing', CURRENT_TIMESTAMP)",
        (1, track_id),
    )
    conn.commit()

    c = TestClient(app)
    res = c.post(
        "/api/liquidsoap/played",
        json={
            "station_id": 1,
            "title": "Played Song",
            "artist": "Artist P",
            "filename": "C:/music/played.mp3",
        },
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["queue_item_done"] is True

    check_conn = get_connection()
    queue_row = check_conn.cursor().execute(
        "SELECT status, finished_at FROM queue_items WHERE station_id=1 AND track_id=?",
        (track_id,),
    ).fetchone()
    assert str(queue_row["status"]) == "done"
    assert str(queue_row["finished_at"] or "").strip()

    track_row = check_conn.cursor().execute(
        "SELECT play_count, last_played_at FROM tracks WHERE id=?",
        (track_id,),
    ).fetchone()
    assert int(track_row["play_count"]) == 3
    assert str(track_row["last_played_at"] or "").strip()


def test_broadcast_queue_autofill_helper_refills_only_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, last_played_at, exclude_from_autoplay) "
        "VALUES (1, 'Busy Song', 'Artist B', 'music', 'C:/music/busy.mp3', 1, 4, '2026-03-13 10:00:00', 0)"
    )
    busy_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, last_played_at, exclude_from_autoplay) "
        "VALUES (1, 'Fresh Song', 'Artist A', 'music', 'C:/music/fresh.mp3', 1, 0, NULL, 0)"
    )
    fresh_track_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, file_path, is_active, play_count, last_played_at, exclude_from_autoplay) "
        "VALUES (1, 'Blocked Song', 'Artist C', 'music', 'C:/music/blocked.mp3', 1, 0, NULL, 1)"
    )
    conn.commit()

    first = ensure_broadcast_queue_filled(conn, station_id=1, refill_size=10)
    second = ensure_broadcast_queue_filled(conn, station_id=1, refill_size=10)

    assert [(int(row["track_id"]), str(row["status"])) for row in first[:2]] == [
        (fresh_track_id, "pending"),
        (busy_track_id, "pending"),
    ]
    assert [(int(row["track_id"]), str(row["status"])) for row in second[:2]] == [
        (fresh_track_id, "pending"),
        (busy_track_id, "pending"),
    ]
    rows = conn.cursor().execute(
        "SELECT track_id, status FROM queue_items WHERE station_id=1 ORDER BY position ASC, id ASC"
    ).fetchall()
    assert [(int(row["track_id"]), str(row["status"])) for row in rows] == [
        (fresh_track_id, "pending"),
        (busy_track_id, "pending"),
    ]
