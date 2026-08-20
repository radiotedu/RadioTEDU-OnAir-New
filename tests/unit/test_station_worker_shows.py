# tests/unit/test_station_worker_shows.py
"""Tests for worker show lifecycle integration."""
from datetime import datetime, timedelta, timezone

from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.queue_repo import QueueRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.station_repo import StationRepository


class _FakeRuntimeRegistry:
    def __init__(self):
        self.started: list[dict] = []
        self.running: dict[int, bool] = {}
        self.transition_active: dict[int, bool] = {}
        self.branch_health: dict[int, dict[str, bool]] = {}
        self.required_outputs: dict[int, dict[str, bool]] = {}

    def start_station(
        self,
        station_id: int,
        input_uri: str,
        stream_title: str = "",
        stream_artist: str = "",
        track_type: str = "music",
        crossfade_seconds: float | None = None,
    ):
        sid = int(station_id)
        self.started.append({
            "station_id": sid,
            "input_uri": str(input_uri),
            "stream_title": str(stream_title or ""),
            "track_type": str(track_type or "music"),
        })
        self.running[sid] = True
        return {"station_id": sid, "running": True}

    def status(self, station_id: int):
        sid = int(station_id)
        is_running = bool(self.running.get(sid, False))
        branch_health = self.branch_health.get(sid, {"local": is_running})
        required_outputs = self.required_outputs.get(sid, {"local": True})
        return {
            "station_id": sid,
            "running": is_running,
            "transition_active": bool(self.transition_active.get(sid, False)),
            "branch_health": branch_health,
            "required_outputs": required_outputs,
        }


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    station_id = StationRepository(conn).create("Test FM")
    # Create a track for the queue
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, file_path, duration, is_active, track_type) "
        "VALUES (?, 'Test', 'Artist', 'test.mp3', 180.0, 1, 'music')",
        (station_id,),
    )
    conn.commit()
    conn.close()
    return station_id


def _make_worker(station_id, runtime=None):
    return StationWorker(
        station_id=station_id,
        worker_id="test-worker",
        runtime_registry=runtime,
    )


def _create_show_with_session(conn, station_id, status="live", intro_path=None):
    show_id = ShowRepository(conn).create(station_id, "Test Show", intro_path=intro_path)
    session_repo = ShowSessionRepository(conn)
    session_id = session_repo.create(show_id, station_id, user_id=1)
    if status != "preparing":
        session_repo.update_status(session_id, status)
    return show_id, session_id


def test_process_once_suppresses_ads_during_live(tmp_path, monkeypatch):
    """During a live show, ad auto-fire should be suppressed."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="live")
    # Add a due ad
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ad_break_items (station_id, track_id, due_at, status, priority) "
        "VALUES (?, 1, CURRENT_TIMESTAMP, 'pending', 0)",
        (station_id,),
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (?, 'hourly_ad_enabled', 'true') "
        "ON CONFLICT(station_id, key) DO UPDATE SET value='true'",
        (station_id,),
    )
    conn.commit()
    conn.close()

    # Add queue tracks so worker has something to play
    conn = get_connection()
    QueueRepository(conn).enqueue(station_id, 1, "t1")
    conn.close()

    result = worker.process_once()
    # Should play from queue (manual), NOT ads
    assert result["source"] in ("manual", "playing"), f"Expected manual/playing, got {result}"


def test_process_once_going_live_holds_when_playing(tmp_path, monkeypatch):
    """When going_live and a track is currently playing, worker should hold."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="going_live")
    # Simulate a currently playing queue item
    queue = QueueRepository(conn)
    item_id = queue.enqueue(station_id, 1, "playing-track")
    queue.mark_playing(item_id)
    # Set started_at to now so it's not expired
    cur = conn.cursor()
    cur.execute(
        "UPDATE queue_items SET started_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), item_id),
    )
    conn.commit()
    conn.close()

    runtime.running[station_id] = True
    result = worker.process_once()
    assert result.get("reason") == "waiting_for_track"


def test_process_once_going_live_plays_intro_when_idle(tmp_path, monkeypatch):
    """When going_live and no track playing, should start intro."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    intro_file = tmp_path / "intro.mp3"
    intro_file.write_bytes(b"fake audio")
    _create_show_with_session(conn, station_id, status="going_live",
                              intro_path=str(intro_file))
    conn.close()

    result = worker.process_once()
    assert result.get("source") == "show_intro"
    # Verify intro was started via runtime
    assert len(runtime.started) >= 1
    assert str(intro_file) in runtime.started[-1]["input_uri"]


def test_process_once_going_live_skips_to_live_without_intro(tmp_path, monkeypatch):
    """If show has no intro_path, go directly from going_live to live."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="going_live")
    # Add queue tracks
    QueueRepository(conn).enqueue(station_id, 1, "t1")
    conn.close()

    result = worker.process_once()
    # Should transition to live and fall through to host queue
    conn = get_connection()
    session = ShowSessionRepository(conn).get_active_for_station(station_id)
    conn.close()
    assert session["status"] == "live"


def test_process_once_intro_playing_transitions_to_live(tmp_path, monkeypatch):
    """When intro finishes playing, transition to live."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="intro_playing")
    conn.close()

    # Runtime says NOT running (intro finished)
    runtime.running[station_id] = False
    result = worker.process_once()

    conn = get_connection()
    session = ShowSessionRepository(conn).get_active_for_station(station_id)
    conn.close()
    assert session["status"] == "live"


def test_process_once_outro_playing_ends_session(tmp_path, monkeypatch):
    """When outro finishes, session should be ended."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="outro_playing")
    conn.close()

    runtime.running[station_id] = False
    result = worker.process_once()
    assert result.get("source") == "show_ended"

    conn = get_connection()
    session = ShowSessionRepository(conn).get_active_for_station(station_id)
    conn.close()
    assert session is None  # ended


def test_process_once_on_break_transitions_to_break_intro(tmp_path, monkeypatch):
    """When on_break and no more ads, play break_intro."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    break_intro = tmp_path / "break_intro.mp3"
    break_intro.write_bytes(b"fake audio")
    show_id, session_id = _create_show_with_session(conn, station_id, status="on_break")
    ShowRepository(conn).update(show_id, break_intro_path=str(break_intro))
    conn.close()

    # No due ads
    result = worker.process_once()
    assert result.get("source") == "show_break_intro"

    conn = get_connection()
    session = ShowSessionRepository(conn).get_active_for_station(station_id)
    conn.close()
    assert session["status"] == "break_intro"


def test_process_once_preparing_runs_normal_automation(tmp_path, monkeypatch):
    """Preparing state should not interfere with normal automation."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    _create_show_with_session(conn, station_id, status="preparing")
    QueueRepository(conn).enqueue(station_id, 1, "auto-track")
    conn.close()

    result = worker.process_once()
    # Normal automation: should play from queue
    assert result["source"] in ("manual", "none")


def test_process_once_live_ads_suppressed_not_on_break(tmp_path, monkeypatch):
    """Ad auto-fire is suppressed during 'live', but NOT during 'on_break'."""
    station_id = _setup(tmp_path, monkeypatch)
    runtime = _FakeRuntimeRegistry()
    worker = _make_worker(station_id, runtime)

    conn = get_connection()
    show_id, session_id = _create_show_with_session(conn, station_id, status="on_break")
    # Add a due ad
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ad_break_items (station_id, track_id, due_at, status, priority) "
        "VALUES (?, 1, CURRENT_TIMESTAMP, 'pending', 0)",
        (station_id,),
    )
    cur.execute(
        "INSERT INTO station_settings (station_id, key, value) VALUES (?, 'hourly_ad_enabled', 'true') "
        "ON CONFLICT(station_id, key) DO UPDATE SET value='true'",
        (station_id,),
    )
    conn.commit()
    conn.close()

    result = worker.process_once()
    # During on_break with due ad, should play ad (source == "ads")
    assert result["source"] == "ads", f"Expected ads, got {result}"
