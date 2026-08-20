from app.db import get_connection, init_db
from app.engine.playout_state import PlayoutStateService, reconcile_all_startup
from app.repositories.queue_repo import QueueRepository
from app.repositories.station_repo import StationRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository


def test_playout_transitions_are_persisted_once_per_state_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    svc = PlayoutStateService(conn)

    svc.set_current(1, "manual", 41, reason="queue_start")
    svc.set_current(1, "manual", 41, reason="duplicate_tick")
    svc.set_current(1, "none", None, reason="track_complete")

    transitions = list(reversed(svc.list_recent(1)))
    assert len(transitions) == 2
    assert transitions[0]["from_source"] == "none"
    assert transitions[0]["to_source"] == "manual"
    assert transitions[0]["to_item_id"] == 41
    assert transitions[0]["reason"] == "queue_start"
    assert transitions[1]["from_source"] == "manual"
    assert transitions[1]["to_source"] == "none"
    assert transitions[1]["reason"] == "track_complete"


def test_reconcile_stale_playing_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    queue = QueueRepository(conn)
    item_id = queue.enqueue(1, 99, "stale-1")
    queue.mark_playing(item_id)
    svc = PlayoutStateService(conn)
    svc.reconcile_startup(station_id=1)
    cur = conn.cursor()
    cur.execute("SELECT status FROM queue_items WHERE id=?", (item_id,))
    assert cur.fetchone()["status"] == "pending"


def test_reconcile_all_startup_resets_all_playing_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    queue = QueueRepository(conn)
    queue_item_id = queue.enqueue(1, 123, "stale-all-queue")
    queue.mark_playing(queue_item_id)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ad_break_items (station_id, track_id, due_at, status, priority) VALUES (1, 501, CURRENT_TIMESTAMP, 'playing', 0)"
    )
    ad_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO schedule_items (station_id, track_id, play_at, window_end, status) VALUES (1, 601, CURRENT_TIMESTAMP, NULL, 'playing')"
    )
    schedule_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO playout_state (station_id, current_source, current_item_id) VALUES (1, 'manual', ?)",
        (queue_item_id,),
    )
    conn.commit()

    summary = reconcile_all_startup(conn)
    assert summary["queue_requeued"] >= 1
    assert summary["ad_requeued"] >= 1
    assert summary["schedule_requeued"] >= 1
    assert summary["playout_reset"] >= 1

    cur.execute("SELECT status FROM queue_items WHERE id=?", (queue_item_id,))
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT status FROM ad_break_items WHERE id=?", (ad_id,))
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT status FROM schedule_items WHERE id=?", (schedule_id,))
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT current_source, current_item_id FROM playout_state WHERE station_id=1")
    row = cur.fetchone()
    assert row["current_source"] == "none"
    assert row["current_item_id"] is None
    transition = conn.execute(
        "SELECT * FROM playout_transitions WHERE station_id=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert transition["from_source"] == "manual"
    assert transition["to_source"] == "none"
    assert transition["reason"] == "startup_reconcile"


def test_reconcile_all_startup_ends_stale_show_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    station_id = StationRepository(conn).create("Test FM")
    show_id = ShowRepository(conn).create(station_id, "Morning Show")
    session_repo = ShowSessionRepository(conn)
    s_id = session_repo.create(show_id, station_id, user_id=1)
    session_repo.update_status(s_id, "live")

    # A session still in "preparing" should NOT be ended
    station_id2 = StationRepository(conn).create("Test AM")
    show_id2 = ShowRepository(conn).create(station_id2, "Evening Show")
    s_id2 = session_repo.create(show_id2, station_id2, user_id=1)

    summary = reconcile_all_startup(conn)
    assert summary["show_sessions_ended"] >= 1

    session = session_repo.get(s_id)
    assert session["status"] == "ended"
    assert session["ended_at"] is not None

    session2 = session_repo.get(s_id2)
    assert session2["status"] == "preparing"


def test_reconcile_all_startup_backfills_missing_track_durations(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()

    audio_file = tmp_path / "startup-duration.mp3"
    audio_file.write_bytes(b"ID3")

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (station_id, title, artist, file_path, duration, is_active, track_type) "
        "VALUES (?, ?, ?, ?, 0, 1, 'music')",
        (1, "Startup Duration", "Tester", str(audio_file)),
    )
    track_id = int(cur.lastrowid)
    conn.commit()

    monkeypatch.setattr(
        "app.engine.playout_state.audio_processing.probe_duration",
        lambda file_path, **_kwargs: 284.25 if str(file_path) == str(audio_file) else 0.0,
    )

    summary = reconcile_all_startup(conn)
    assert summary["track_durations_backfilled"] == 1

    cur.execute("SELECT duration FROM tracks WHERE id=?", (track_id,))
    row = cur.fetchone()
    assert row is not None
    assert float(row["duration"] or 0.0) == 284.25
