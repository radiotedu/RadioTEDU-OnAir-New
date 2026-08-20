import sqlite3

from app.db import _SCHEMA_VERSION, get_connection, init_db


_LEGACY_DJ_SHOW_PERMISSIONS = {
    "show.broadcast",
    "show.queue_edit",
    "show.jingle_manage",
    "show.break_control",
    "show.end",
}


def test_rbac_tables_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "role_templates" in tables
    assert "role_template_permissions" in tables
    assert "user_role_assignments" in tables
    assert "show_assignment_permissions" in tables


def test_rbac_tables_upgrade_from_schema_version_5(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'dj',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT,
                avatar_url TEXT
            );
            CREATE TABLE shows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL REFERENCES stations(id),
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE show_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'dj',
                UNIQUE(show_id, user_id)
            );
            CREATE TABLE show_assignment_permissions (
                show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                permission_key TEXT NOT NULL,
                PRIMARY KEY(show_id, user_id, permission_key)
            );
            INSERT INTO stations (id, name) VALUES (1, 'Station 1');
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (1, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (2, 'dj-2', 'DJ 2', 'hash', 'dj', 1);
            INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
            INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 1, 'dj');
            INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
                VALUES (1, 1, 'broadcast');
            INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
                VALUES (1, 2, 'backstage');
            PRAGMA user_version=5;
            """
        )
        conn.commit()
    finally:
        conn.close()

    init_db()

    conn = get_connection()
    try:
        cur = conn.cursor()
        tables = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        fk_tables = {
            row[2]
            for row in cur.execute(
                "PRAGMA foreign_key_list(show_assignment_permissions)"
            ).fetchall()
        }
        rows = [
            (int(row[0]), int(row[1]), str(row[2]))
            for row in cur.execute(
                "SELECT show_id, user_id, permission_key "
                "FROM show_assignment_permissions "
                "ORDER BY user_id, permission_key"
            ).fetchall()
        ]
        cur.execute("PRAGMA user_version")
        version = int(cur.fetchone()[0])
    finally:
        conn.close()

    assert version == _SCHEMA_VERSION
    assert "role_templates" in tables
    assert "role_template_permissions" in tables
    assert "user_role_assignments" in tables
    assert "show_assignment_permissions" in tables
    assert fk_tables == {"show_assignments"}
    assert rows == [(1, 1, "broadcast")] + [
        (1, 1, permission_key) for permission_key in sorted(_LEGACY_DJ_SHOW_PERMISSIONS)
    ]


def test_show_assignment_permissions_cascades_when_assignment_removed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, display_name, password_hash, role, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dj-1", "DJ 1", "hash", "dj", 1),
        )
        user_id = cur.lastrowid
        cur.execute(
            "INSERT INTO shows (station_id, name, is_active) VALUES (?, ?, ?)",
            (1, "Morning Show", 1),
        )
        show_id = cur.lastrowid
        cur.execute(
            "INSERT INTO show_assignments (show_id, user_id, role) VALUES (?, ?, ?)",
            (show_id, user_id, "dj"),
        )
        cur.execute(
            "INSERT INTO show_assignment_permissions (show_id, user_id, permission_key) "
            "VALUES (?, ?, ?)",
            (show_id, user_id, "broadcast"),
        )
        conn.commit()

        cur.execute("DELETE FROM show_assignments WHERE show_id = ? AND user_id = ?", (show_id, user_id))
        conn.commit()

        cur.execute(
            "SELECT COUNT(*) FROM show_assignment_permissions "
            "WHERE show_id = ? AND user_id = ?",
            (show_id, user_id),
        )
        remaining = int(cur.fetchone()[0])
    finally:
        conn.close()

    assert remaining == 0


def test_rbac_tables_resume_interrupted_permission_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'dj',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT,
                avatar_url TEXT
            );
            CREATE TABLE shows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL REFERENCES stations(id),
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE show_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'dj',
                UNIQUE(show_id, user_id)
            );
            CREATE TABLE show_assignment_permissions_old (
                show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                permission_key TEXT NOT NULL,
                PRIMARY KEY(show_id, user_id, permission_key)
            );
            INSERT INTO stations (id, name) VALUES (1, 'Station 1');
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (1, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (2, 'dj-2', 'DJ 2', 'hash', 'dj', 1);
            INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
            INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 1, 'dj');
            INSERT INTO show_assignment_permissions_old (show_id, user_id, permission_key)
                VALUES (1, 1, 'broadcast');
            INSERT INTO show_assignment_permissions_old (show_id, user_id, permission_key)
                VALUES (1, 2, 'backstage');
            PRAGMA user_version=5;
            """
        )
        conn.commit()
    finally:
        conn.close()

    init_db()

    conn = get_connection()
    try:
        cur = conn.cursor()
        fk_tables = {
            row[2]
            for row in cur.execute(
                "PRAGMA foreign_key_list(show_assignment_permissions)"
            ).fetchall()
        }
        rows = [
            (int(row[0]), int(row[1]), str(row[2]))
            for row in cur.execute(
                "SELECT show_id, user_id, permission_key "
                "FROM show_assignment_permissions "
                "ORDER BY user_id, permission_key"
            ).fetchall()
        ]
        cur.execute("PRAGMA user_version")
        version = int(cur.fetchone()[0])
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='show_assignment_permissions_old'"
        )
        old_exists = cur.fetchone() is not None
    finally:
        conn.close()

    assert version == _SCHEMA_VERSION
    assert fk_tables == {"show_assignments"}
    assert rows == [(1, 1, "broadcast")] + [
        (1, 1, permission_key) for permission_key in sorted(_LEGACY_DJ_SHOW_PERMISSIONS)
    ]
    assert not old_exists
