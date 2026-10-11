import sqlite3

from app.db import (
    _migrate_ad_break_items,
    _migrate_queue_items,
    get_connection,
    init_db,
)
from app.repositories.queue_repo import QueueRepository


def test_enqueue_and_next_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = QueueRepository(get_connection())
    item_id = repo.enqueue(station_id=1, track_id=77, dedupe_key="a1")
    assert item_id > 0
    nxt = repo.next_pending(station_id=1)
    assert nxt["track_id"] == 77
    repo.mark_playing(item_id)
    repo.mark_done(item_id)


def test_enqueue_or_get_existing_dedupes_pending_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = QueueRepository(get_connection())

    id1, created1 = repo.enqueue_or_get_existing(
        station_id=1, track_id=77, dedupe_key="q:1:77"
    )
    id2, created2 = repo.enqueue_or_get_existing(
        station_id=1, track_id=77, dedupe_key="q:1:77"
    )

    assert created1 is True
    assert created2 is False
    assert id1 == id2


def test_defer_releases_playing_item_and_retries_after_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    repo = QueueRepository(conn)
    deferred_id = repo.enqueue(1, 77, dedupe_key="retry-song")
    ready_id = repo.enqueue(1, 78, dedupe_key="ready-song")
    original = conn.execute(
        "SELECT position, dedupe_key FROM queue_items WHERE id=?", (deferred_id,)
    ).fetchone()
    repo.mark_playing(deferred_id)

    assert repo.defer(deferred_id, 3600, error="decoder restart failed") == 1

    deferred = conn.execute(
        "SELECT status, position, dedupe_key, started_at, retry_after, retry_count, last_error "
        "FROM queue_items WHERE id=?",
        (deferred_id,),
    ).fetchone()
    assert deferred["status"] == "pending"
    assert deferred["position"] == original["position"]
    assert deferred["dedupe_key"] == original["dedupe_key"]
    assert deferred["started_at"] is None
    assert deferred["retry_after"] is not None
    assert deferred["retry_count"] == 1
    assert deferred["last_error"] == "decoder restart failed"
    assert repo.current_playing(1) is None
    assert repo.next_pending(1)["id"] == ready_id

    existing_id, created = repo.enqueue_or_get_existing(
        station_id=1, track_id=77, dedupe_key="retry-song"
    )
    assert (existing_id, created) == (deferred_id, False)

    conn.execute(
        "UPDATE queue_items SET retry_after=datetime(CURRENT_TIMESTAMP, '-1 seconds') "
        "WHERE id=?",
        (deferred_id,),
    )
    conn.commit()
    assert repo.next_pending(1)["id"] == deferred_id


def test_defer_bounds_retry_metadata_and_only_updates_playing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    repo = QueueRepository(conn)
    item_id = repo.enqueue(1, 77)
    assert repo.defer(item_id, 60, error="not playing") == 0

    repo.mark_playing(item_id)
    conn.execute("UPDATE queue_items SET retry_count=10000 WHERE id=?", (item_id,))
    conn.commit()
    assert repo.defer(item_id, 60, error="x" * 1000) == 1

    item = conn.execute(
        "SELECT retry_count, last_error FROM queue_items WHERE id=?", (item_id,)
    ).fetchone()
    assert item["retry_count"] == 10000
    assert len(item["last_error"]) == 512


def test_legacy_queue_and_ad_schema_migrations_add_retry_columns_idempotently():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE queue_items (id INTEGER PRIMARY KEY, station_id INTEGER, "
        "track_id INTEGER, position INTEGER, status TEXT)"
    )
    conn.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) "
        "VALUES (1, 77, 1, 'pending')"
    )
    conn.execute(
        "CREATE TABLE ad_break_items (id INTEGER PRIMARY KEY, station_id INTEGER, "
        "track_id INTEGER, due_at TEXT, status TEXT, priority INTEGER)"
    )
    conn.execute(
        "INSERT INTO ad_break_items (station_id, track_id, due_at, status, priority) "
        "VALUES (1, 77, '2026-01-01 00:00:00', 'pending', 0)"
    )
    cursor = conn.cursor()

    _migrate_queue_items(cursor)
    _migrate_ad_break_items(cursor)
    _migrate_queue_items(cursor)
    _migrate_ad_break_items(cursor)

    for table in ("queue_items", "ad_break_items"):
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert {"retry_after", "retry_count", "last_error"}.issubset(columns)
        row = conn.execute(
            f"SELECT status, retry_after, retry_count, last_error FROM {table}"
        ).fetchone()
        assert row == ("pending", None, 0, "")
    conn.close()
