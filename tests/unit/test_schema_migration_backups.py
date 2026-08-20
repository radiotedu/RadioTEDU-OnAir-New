import json
import sqlite3

import pytest

import app.db as database


def _legacy_database(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT DEFAULT '', "
            "artist TEXT DEFAULT '', musicbrainz_recordingid TEXT DEFAULT '')"
        )
        conn.commit()
    finally:
        conn.close()


def _columns(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _backup_files(path):
    backup_directory = path.parent / "schema-backups"
    if not backup_directory.exists():
        return []
    return sorted(
        item
        for item in backup_directory.iterdir()
        if item.is_file() and not item.name.endswith(("-wal", "-shm"))
    )


def test_new_database_and_repeated_initialization_do_not_create_backups(tmp_path, monkeypatch):
    db_path = tmp_path / "onair.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    database.init_db()
    database.init_db()

    assert _backup_files(db_path) == []
    assert not (db_path.parent / "schema-migration-backups.json").exists()


def test_pending_schema_migration_creates_verified_backup_and_ledger(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    database.init_db()

    backups = _backup_files(db_path)
    assert len(backups) == 1
    backup = backups[0]
    assert "file_path" not in _columns(backup, "tracks")
    assert "file_path" in _columns(db_path, "tracks")
    with sqlite3.connect(str(backup)) as conn:
        assert conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"

    ledger = json.loads(
        (db_path.parent / "schema-migration-backups.json").read_text(encoding="utf-8")
    )
    assert ledger["version"] == 1
    assert ledger["records"][0]["backup_path"] == str(backup.resolve())
    assert ledger["records"][0]["database_path"] == str(db_path.resolve())


def test_backup_failure_blocks_schema_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    def fail_backup(_path):
        raise OSError("backup storage unavailable")

    monkeypatch.setattr(database, "_backup_database_before_schema_migration", fail_backup)

    with pytest.raises(OSError, match="backup storage unavailable"):
        database.init_db()

    assert "file_path" not in _columns(db_path, "tracks")
    assert _backup_files(db_path) == []


def test_locked_or_corrupt_database_is_not_migrated_or_backed_up(tmp_path, monkeypatch):
    locked_path = tmp_path / "locked.db"
    _legacy_database(locked_path)
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(locked_path))

    lock = sqlite3.connect(str(locked_path), timeout=0)
    lock.execute("BEGIN EXCLUSIVE")
    original_get_connection = database.get_connection
    monkeypatch.setattr(
        database,
        "get_connection",
        lambda: sqlite3.connect(str(locked_path), timeout=0),
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.init_db()
    finally:
        lock.rollback()
        lock.close()

    assert "file_path" not in _columns(locked_path, "tracks")
    assert _backup_files(locked_path) == []

    corrupt_path = tmp_path / "corrupt.db"
    corrupt_bytes = b"not a sqlite database"
    corrupt_path.write_bytes(corrupt_bytes)
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(corrupt_path))
    monkeypatch.setattr(database, "get_connection", original_get_connection)

    with pytest.raises(sqlite3.DatabaseError):
        database.init_db()

    assert corrupt_path.read_bytes() == corrupt_bytes
    assert _backup_files(corrupt_path) == []


def test_completed_migration_is_idempotent_and_does_not_create_another_backup(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    database.init_db()
    before = _backup_files(db_path)
    database.init_db()

    assert len(before) == 1
    assert _backup_files(db_path) == before


def test_schema_backup_retention_is_bounded_and_recorded(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)
    monkeypatch.setenv("RADIOTEDU_SCHEMA_BACKUP_RETENTION", "1")

    database._backup_database_before_schema_migration(db_path)
    database._backup_database_before_schema_migration(db_path)

    backups = _backup_files(db_path)
    assert len(backups) == 1
    ledger = json.loads(
        (db_path.parent / "schema-migration-backups.json").read_text(encoding="utf-8")
    )
    assert len(ledger["records"]) == 1
    assert ledger["records"][0]["backup_path"] == str(backups[0].resolve())


def test_v17_migration_repairs_only_orphaned_rbac_assignments(tmp_path, monkeypatch):
    db_path = tmp_path / "onair.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    database.init_db()

    with sqlite3.connect(str(db_path)) as conn:
        user_id = int(conn.execute("SELECT MIN(id) FROM users").fetchone()[0])
        role_id = int(conn.execute("SELECT MIN(id) FROM role_templates").fetchone()[0])
        missing_user_id = int(conn.execute("SELECT MAX(id) FROM users").fetchone()[0]) + 1000
        missing_role_id = (
            int(conn.execute("SELECT MAX(id) FROM role_templates").fetchone()[0]) + 1000
        )
        valid_assignments = set(
            conn.execute(
                "SELECT user_id, role_template_id FROM user_role_assignments"
            ).fetchall()
        )
        conn.execute(
            "INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (?, ?)",
            (missing_user_id, role_id),
        )
        conn.execute(
            "INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (?, ?)",
            (user_id, missing_role_id),
        )
        conn.execute(
            "UPDATE system_settings SET value='16' WHERE key='__schema_bootstrapped__'"
        )
        conn.execute("DELETE FROM system_settings WHERE key='__rbac_integrity_repair_v17__'")
        conn.execute("PRAGMA user_version=16")
        conn.commit()

    database.init_db()

    backups = _backup_files(db_path)
    assert len(backups) == 1
    with sqlite3.connect(str(backups[0])) as backup:
        assert len(backup.execute("PRAGMA foreign_key_check").fetchall()) == 2

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert set(
            conn.execute(
                "SELECT user_id, role_template_id FROM user_role_assignments"
            ).fetchall()
        ) == valid_assignments
        assert conn.execute("PRAGMA user_version").fetchone()[0] == database._SCHEMA_VERSION
        marker = conn.execute(
            "SELECT value FROM system_settings "
            "WHERE key='__rbac_integrity_repair_v17__'"
        ).fetchone()[0]
        assert json.loads(marker) == {"removed_assignments": 2, "schema_version": 17}

    database.init_db()
    assert _backup_files(db_path) == backups


def test_v17_migration_blocks_unrelated_foreign_key_damage(tmp_path, monkeypatch):
    db_path = tmp_path / "onair.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    database.init_db()

    with sqlite3.connect(str(db_path)) as conn:
        missing_role_id = (
            int(conn.execute("SELECT MAX(id) FROM role_templates").fetchone()[0]) + 1000
        )
        conn.execute(
            "INSERT INTO role_template_permissions (role_template_id, permission_key) "
            "VALUES (?, ?)",
            (missing_role_id, "stations.view"),
        )
        conn.execute(
            "UPDATE system_settings SET value='16' WHERE key='__schema_bootstrapped__'"
        )
        conn.execute("PRAGMA user_version=16")
        conn.commit()

    with pytest.raises(
        RuntimeError,
        match="unexpected foreign-key violations: role_template_permissions->role_templates",
    ):
        database.init_db()

    assert len(_backup_files(db_path)) == 1
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert len(violations) == 1
        assert violations[0][0] == "role_template_permissions"
