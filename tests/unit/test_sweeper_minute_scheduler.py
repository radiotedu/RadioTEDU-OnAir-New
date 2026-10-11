from app.db import get_connection, init_db
from app.engine.station_worker import StationWorker
from app.repositories.settings_repo import SettingsRepository


def _add_track(conn, station_id: int, title: str, track_type: str, duration: float) -> int:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tracks (station_id, title, artist, track_type, duration, file_path, is_active, play_count, exclude_from_autoplay) "
        "VALUES (?, ?, 'Test', ?, ?, ?, 1, 0, 0)",
        (station_id, title, track_type, duration, f"C:/audio/{station_id}-{title}.mp3"),
    )
    return int(cursor.lastrowid)


def test_minute_scheduler_waits_for_threshold_and_never_borrows_other_station_jingle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()

    pop_jingle = _add_track(conn, 1, "Pop Jingle", "jingle", 8)
    rock_jingle = _add_track(conn, 2, "Rock Jingle", "jingle", 8)
    song_a = _add_track(conn, 2, "Rock A", "music", 900)
    song_b = _add_track(conn, 2, "Rock B", "music", 850)
    song_c = _add_track(conn, 2, "Rock C", "music", 240)
    upcoming = _add_track(conn, 2, "Rock D", "music", 300)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 1, 'done')",
        (song_a,),
    )
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 2, 'done')",
        (song_b,),
    )
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 3, 'playing')",
        (song_c,),
    )
    playing_item = int(cursor.lastrowid)
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 4, 'pending')",
        (upcoming,),
    )
    SettingsRepository(conn).upsert_station(
        2,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "30",
            "sweeper_interval_unit": "minutes",
            "sweeper_baseline_queue_id": "0",
            "sweeper_mode": "ordered",
        },
    )
    conn.commit()

    worker = StationWorker(station_id=2)
    assert worker._music_seconds_since_last_jingle() == 1990.0
    assert worker._maybe_insert_sweeper_jingle() is True

    rows = conn.cursor().execute(
        "SELECT q.track_id, q.status, t.track_type, t.station_id "
        "FROM queue_items q JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=2 ORDER BY q.position, q.id"
    ).fetchall()
    pending_jingles = [row for row in rows if row["status"] == "pending" and row["track_type"] == "jingle"]
    assert len(pending_jingles) == 1
    assert int(pending_jingles[0]["track_id"]) == rock_jingle
    assert int(pending_jingles[0]["station_id"]) == 2
    assert int(pending_jingles[0]["track_id"]) != pop_jingle
    assert conn.cursor().execute(
        "SELECT status FROM queue_items WHERE id=?", (playing_item,)
    ).fetchone()["status"] == "playing"


def test_minute_scheduler_does_not_insert_before_completed_song_duration_crosses_target(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    jingle = _add_track(conn, 1, "Pop Jingle", "jingle", 8)
    song = _add_track(conn, 1, "Pop Song", "music", 1799)
    upcoming = _add_track(conn, 1, "Next Pop Song", "music", 300)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'playing')",
        (song,),
    )
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (upcoming,),
    )
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "30",
            "sweeper_interval_unit": "minutes",
            "sweeper_baseline_queue_id": "0",
        },
    )
    conn.commit()

    worker = StationWorker(station_id=1)
    assert worker._maybe_insert_sweeper_jingle() is False
    count = conn.cursor().execute(
        "SELECT COUNT(*) AS count FROM queue_items WHERE track_id=?", (jingle,)
    ).fetchone()["count"]
    assert int(count) == 0


def test_due_jingle_is_followed_by_global_ad_and_never_borrowed_from_another_station(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    jingle = _add_track(conn, 1, "Pop Jingle", "jingle", 8)
    ad = _add_track(conn, 1, "Global Ad", "ad", 12)
    _add_track(conn, 2, "Other Ad", "ad", 9)
    song = _add_track(conn, 1, "Played Song", "music", 1800)
    upcoming = _add_track(conn, 1, "Upcoming Song", "music", 300)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'done')",
        (song,),
    )
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
        (upcoming,),
    )
    SettingsRepository(conn).upsert_station(
        1,
        {
            "sweeper_enabled": "true",
            "sweeper_interval": "30",
            "sweeper_interval_unit": "minutes",
            "sweeper_baseline_queue_id": "0",
            "sweeper_mode": "ordered",
        },
    )
    conn.commit()

    worker = StationWorker(station_id=1)
    assert worker._maybe_insert_sweeper_jingle() is True

    pending = conn.execute(
        "SELECT q.track_id, t.track_type FROM queue_items q "
        "JOIN tracks t ON t.id=q.track_id "
        "WHERE q.station_id=1 AND q.status='pending' "
        "ORDER BY q.position, q.id"
    ).fetchall()
    assert [(int(row["track_id"]), row["track_type"]) for row in pending] == [
        (jingle, "jingle"),
        (ad, "ad"),
        (upcoming, "music"),
    ]
