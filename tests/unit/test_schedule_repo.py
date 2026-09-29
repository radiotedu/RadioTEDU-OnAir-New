import sqlite3

from app.db import _migrate_schedule_items
from app.repositories.schedule_repo import ScheduleRepository


def _schedule_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schedule_items ("
        "id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, "
        "play_at TEXT NOT NULL, window_end TEXT, event_name TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'pending', retry_after TEXT, "
        "retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')"
    )
    return conn


def test_defer_preserves_schedule_identity_and_next_ready_retries_after_deadline():
    conn = _schedule_db()
    conn.executemany(
        "INSERT INTO schedule_items "
        "(id, station_id, track_id, play_at, window_end, event_name, status) "
        "VALUES (?, 1, ?, ?, ?, ?, 'pending')",
        [
            (10, 77, "2020-01-01 10:00:00", "2099-01-01 00:00:00", "Hourly ID"),
            (11, 78, "2020-01-01 11:00:00", None, "Next item"),
        ],
    )
    conn.commit()
    repo = ScheduleRepository(conn)
    original = dict(conn.execute("SELECT * FROM schedule_items WHERE id=10").fetchone())

    assert repo.next_ready(1)["id"] == 10
    repo.mark_playing(10)
    assert repo.defer(10, 3600, error="temporary producer failure") == 1

    deferred = dict(conn.execute("SELECT * FROM schedule_items WHERE id=10").fetchone())
    for field in ("id", "station_id", "track_id", "play_at", "window_end", "event_name"):
        assert deferred[field] == original[field]
    assert deferred["status"] == "pending"
    assert deferred["retry_after"] is not None
    assert deferred["retry_count"] == 1
    assert deferred["last_error"] == "temporary producer failure"
    assert repo.next_ready(1)["id"] == 11

    # The row remains unique and returns to its original schedule position.
    conn.execute(
        "UPDATE schedule_items SET retry_after=datetime(CURRENT_TIMESTAMP, '-1 seconds') "
        "WHERE id=10"
    )
    conn.commit()
    assert repo.next_ready(1)["id"] == 10


def test_defer_bounds_metadata_and_only_releases_playing_schedules():
    conn = _schedule_db()
    conn.execute(
        "INSERT INTO schedule_items (id, station_id, track_id, play_at) "
        "VALUES (1, 1, 77, '2020-01-01 00:00:00')"
    )
    conn.commit()
    repo = ScheduleRepository(conn)
    assert repo.defer(1, 60, error="not playing") == 0

    repo.mark_playing(1)
    conn.execute("UPDATE schedule_items SET retry_count=10000 WHERE id=1")
    conn.commit()
    assert repo.defer(1, 50 * 24 * 60 * 60, error="x" * 1000) == 1
    item = conn.execute(
        "SELECT retry_after, retry_count, last_error FROM schedule_items WHERE id=1"
    ).fetchone()
    assert conn.execute(
        "SELECT datetime(retry_after) <= datetime(CURRENT_TIMESTAMP, '+30 days') "
        "FROM schedule_items WHERE id=1"
    ).fetchone()[0]
    assert item["retry_count"] == 10000
    assert len(item["last_error"]) == 512


def test_legacy_schedule_migration_adds_retry_columns_idempotently():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE schedule_items (id INTEGER PRIMARY KEY, station_id INTEGER, "
        "track_id INTEGER, play_at TEXT, window_end TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO schedule_items VALUES (7, 3, 91, '2026-09-29 08:00:00', NULL, 'pending')"
    )
    cursor = conn.cursor()

    _migrate_schedule_items(cursor)
    _migrate_schedule_items(cursor)

    item = conn.execute("SELECT * FROM schedule_items WHERE id=7").fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schedule_items)")}
    assert {"event_name", "retry_after", "retry_count", "last_error"} <= columns
    assert item[0:6] == (7, 3, 91, "2026-09-29 08:00:00", None, "pending")
    assert item[6:] == ("", None, 0, "")
