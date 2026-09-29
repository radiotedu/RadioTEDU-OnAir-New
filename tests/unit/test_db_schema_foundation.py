import sqlite3

from app.db import get_connection, init_db


def test_foundation_tables_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert "station_worker_lease" in names
    assert "playout_state" in names
    assert "schedule_items" in names
    assert "ad_break_items" in names
    assert "command_outbox" in names


def test_connection_supports_a_bounded_best_effort_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    conn = get_connection(timeout_seconds=0.25)
    try:
        busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        conn.close()

    assert busy_timeout == 250


def test_init_db_migrates_legacy_queue_items_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE queue_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    init_db()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(queue_items)")
    names = {row[1] for row in cur.fetchall()}
    assert "status" in names
    assert "enqueued_at" in names
    assert "started_at" in names
    assert "finished_at" in names
    assert "dedupe_key" in names


def test_init_db_repairs_retry_columns_on_version_25_fast_path(
    tmp_path, monkeypatch
):
    import app.db as db

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "v25-retry-repair.db"))
    monkeypatch.setattr(db, "_INITIALIZED_DATABASES", set())
    db.init_db()

    conn = db.get_connection()
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 25
        for table_name in ("queue_items", "ad_break_items", "schedule_items"):
            for column in ("retry_after", "retry_count", "last_error"):
                conn.execute(f"ALTER TABLE {table_name} DROP COLUMN {column}")
        conn.commit()
        assert db._post_version_repairs_needed(conn.cursor()) is True
    finally:
        conn.close()

    # Reopen as a fresh process: the schema version and bootstrap marker still
    # match v25, so post-version repair detection must force the additive migration.
    monkeypatch.setattr(db, "_INITIALIZED_DATABASES", set())
    db.init_db()

    conn = db.get_connection()
    try:
        for table_name in ("queue_items", "ad_break_items", "schedule_items"):
            columns = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            assert {"retry_after", "retry_count", "last_error"} <= columns
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 25
    finally:
        conn.close()


def test_retry_column_migration_rechecks_after_competing_v25_schema_writer(
    tmp_path,
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import app.db as db

    db_path = tmp_path / "concurrent-v25-retry-repair.db"
    writer = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    migrator = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    for table_name in ("queue_items", "ad_break_items", "schedule_items"):
        writer.execute(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)")
    writer.commit()

    # Simulate service A having passed the v25 fast-path check and acquired the
    # schema write lock, after which it adds the retry fields but has not yet
    # committed. Service B's preflight PRAGMA still sees the old schema.
    writer.execute("BEGIN IMMEDIATE")
    for table_name in ("queue_items", "ad_break_items", "schedule_items"):
        for _column, statement in db._RETRY_COLUMN_DEFINITIONS[table_name]:
            writer.execute(statement)
    for table_name in ("queue_items", "ad_break_items", "schedule_items"):
        visible_columns = {
            row[1]
            for row in migrator.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        assert not {"retry_after", "retry_count", "last_error"} & visible_columns

    begin_attempted = threading.Event()

    class _ObservedCursor:
        def __init__(self, conn):
            self.connection = conn
            self._cursor = conn.cursor()

        def execute(self, statement, parameters=()):
            if statement == "BEGIN IMMEDIATE":
                begin_attempted.set()
            return self._cursor.execute(statement, parameters)

    def migrate_all_retry_columns():
        cursor = _ObservedCursor(migrator)
        for table_name in ("queue_items", "ad_break_items", "schedule_items"):
            db._ensure_retry_columns(cursor, table_name)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            migration = executor.submit(migrate_all_retry_columns)
            assert begin_attempted.wait(timeout=3)
            writer.commit()
            migration.result(timeout=5)

        for table_name in ("queue_items", "ad_break_items", "schedule_items"):
            columns = {
                row[1]
                for row in migrator.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            assert {"retry_after", "retry_count", "last_error"} <= columns
    finally:
        writer.close()
        migrator.close()


def test_init_db_is_safe_to_reenter_while_another_writer_holds_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "locked.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    init_db()

    lock_conn = sqlite3.connect(str(db_path), timeout=0.1)
    try:
        lock_conn.execute("BEGIN IMMEDIATE")
        init_db()
    finally:
        lock_conn.rollback()
        lock_conn.close()


def test_init_db_does_not_reopen_verified_database_on_each_request(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "verified.db"))
    db.init_db()

    def unexpected_connection(**_kwargs):
        raise AssertionError("verified database should not reopen SQLite for init_db")

    monkeypatch.setattr(db, "get_connection", unexpected_connection)
    db.init_db()


def test_get_connection_does_not_reassert_wal_for_existing_database(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "wal.db"))
    db.init_db()
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", traced_connect)
    conn = db.get_connection()
    conn.close()

    assert "PRAGMA journal_mode" in statements
    assert "PRAGMA journal_mode=WAL" not in statements
