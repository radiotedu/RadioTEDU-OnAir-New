import sqlite3
import time

from app.auth.permissions import GLOBAL_PERMISSION_KEYS
from app.db import _SCHEMA_VERSION, get_connection, init_db
from app.repositories.rbac_repo import RbacRepository


_V6_BASE_SCHEMA = """
CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'dj',
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE show_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'dj',
    UNIQUE(show_id, user_id)
);
CREATE TABLE role_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE role_template_permissions (
    role_template_id INTEGER NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY(role_template_id, permission_key)
);
CREATE TABLE user_role_assignments (
    user_id INTEGER NOT NULL,
    role_template_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, role_template_id)
);
CREATE TABLE show_assignment_permissions (
    show_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY(show_id, user_id, permission_key),
    FOREIGN KEY(show_id, user_id) REFERENCES show_assignments(show_id, user_id) ON DELETE CASCADE
);
PRAGMA user_version=6;
"""

_LEGACY_SHOW_DJ_PERMISSIONS = {
    "show.broadcast",
    "show.queue_edit",
    "show.jingle_manage",
    "show.break_control",
    "show.end",
}

_LEGACY_SHOW_PRODUCER_PERMISSIONS = {
    "show.queue_edit",
    "show.jingle_manage",
    "show.break_control",
}


def _create_v6_db(db_path, extra_sql: str = "") -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_V6_BASE_SCHEMA)
        if extra_sql.strip():
            conn.executescript(extra_sql)
        conn.commit()
    finally:
        conn.close()


def _role_template_ids(conn) -> dict[str, int]:
    repo = RbacRepository(conn)
    return {row["name"]: int(row["id"]) for row in repo.list_role_templates(include_inactive=True)}


def test_fresh_bootstrap_seeds_legacy_admin(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    init_db()
    init_db()

    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        templates = _role_template_ids(conn)
        admin_roles = repo.list_user_role_ids(1)
        admin_permissions = repo.list_role_permissions(templates["Legacy Admin"])
        admin_effective_permissions = repo.list_effective_global_permissions(1)
    finally:
        conn.close()

    assert set(templates) == {
        "Legacy Admin",
        "Legacy DJ",
        "Legacy Producer",
        "Legacy Viewer",
    }
    assert admin_roles == {templates["Legacy Admin"]}
    assert admin_permissions == set(GLOBAL_PERMISSION_KEYS)
    assert admin_effective_permissions == set(GLOBAL_PERMISSION_KEYS)


def test_pre_v6_upgrade_seeds_legacy_rbac(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
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
            CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
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
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
            INSERT INTO users (id, username, display_name, password_hash, role, is_active)
                VALUES (4, 'viewer-1', 'Viewer 1', 'hash', 'viewer', 1);
            INSERT INTO stations (id, name) VALUES (1, 'Station 1');
            INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
            INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 2, 'dj');
            INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 3, 'producer');
            PRAGMA user_version=5;
            """
        )
        conn.commit()
    finally:
        conn.close()

    init_db()

    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        templates = _role_template_ids(conn)
        user_roles = {
            int(row["user_id"]): int(row["role_template_id"])
            for row in conn.execute(
                "SELECT user_id, role_template_id FROM user_role_assignments ORDER BY user_id, role_template_id"
            ).fetchall()
        }
        dj_permissions = {
            str(row["permission_key"])
            for row in conn.execute(
                "SELECT permission_key FROM show_assignment_permissions WHERE show_id = 1 AND user_id = 2"
            ).fetchall()
        }
        dj_effective_permissions = repo.list_effective_global_permissions(2)
    finally:
        conn.close()

    assert set(templates) == {
        "Legacy Admin",
        "Legacy DJ",
        "Legacy Producer",
        "Legacy Viewer",
    }
    assert user_roles == {
        1: templates["Legacy Admin"],
        2: templates["Legacy DJ"],
        3: templates["Legacy Producer"],
        4: templates["Legacy Viewer"],
    }
    assert dj_permissions == _LEGACY_SHOW_DJ_PERMISSIONS
    assert "library.view" in dj_effective_permissions
    assert "queue.edit" in dj_effective_permissions


def test_partial_user_role_assignments_are_backfilled(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    _create_v6_db(
        db_path,
        """
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (4, 'viewer-1', 'Viewer 1', 'hash', 'viewer', 1);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (1, 'Legacy Admin', 'Legacy Admin compatibility template', 1, 0);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (2, 'Legacy DJ', 'Legacy DJ compatibility template', 1, 0);
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (1, 1);
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (2, 2);
        """,
    )

    init_db()

    conn = get_connection()
    try:
        templates = _role_template_ids(conn)
        rows = [
            (int(row["user_id"]), int(row["role_template_id"]))
            for row in conn.execute(
                "SELECT user_id, role_template_id FROM user_role_assignments "
                "ORDER BY user_id, role_template_id"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert rows == [
        (1, templates["Legacy Admin"]),
        (2, templates["Legacy DJ"]),
        (3, templates["Legacy Producer"]),
        (4, templates["Legacy Viewer"]),
    ]


def test_partial_show_assignment_permissions_are_backfilled(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    _create_v6_db(
        db_path,
        """
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
        INSERT INTO stations (id, name) VALUES (1, 'Station 1');
        INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 2, 'dj');
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 3, 'producer');
        INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
            VALUES (1, 2, 'show.broadcast');
        INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
            VALUES (1, 3, 'show.queue_edit');
        """,
    )

    init_db()

    conn = get_connection()
    try:
        dj_permissions = {
            str(row["permission_key"])
            for row in conn.execute(
                "SELECT permission_key FROM show_assignment_permissions "
                "WHERE show_id = 1 AND user_id = 2 "
                "ORDER BY permission_key"
            ).fetchall()
        }
        producer_permissions = {
            str(row["permission_key"])
            for row in conn.execute(
                "SELECT permission_key FROM show_assignment_permissions "
                "WHERE show_id = 1 AND user_id = 3 "
                "ORDER BY permission_key"
            ).fetchall()
        }
    finally:
        conn.close()

    assert dj_permissions == _LEGACY_SHOW_DJ_PERMISSIONS
    assert producer_permissions == _LEGACY_SHOW_PRODUCER_PERMISSIONS


def test_show_assignment_backfill_ignores_existing_non_show_permissions(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    _create_v6_db(
        db_path,
        """
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
        INSERT INTO stations (id, name) VALUES (1, 'Station 1');
        INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 2, 'dj');
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 3, 'producer');
        INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
            VALUES (1, 2, 'custom.permission');
        """,
    )

    init_db()

    conn = get_connection()
    try:
        dj_permissions = {
            str(row["permission_key"])
            for row in conn.execute(
                "SELECT permission_key FROM show_assignment_permissions "
                "WHERE show_id = 1 AND user_id = 2"
            ).fetchall()
        }
        producer_permissions = {
            str(row["permission_key"])
            for row in conn.execute(
                "SELECT permission_key FROM show_assignment_permissions "
                "WHERE show_id = 1 AND user_id = 3"
            ).fetchall()
        }
    finally:
        conn.close()

    assert _LEGACY_SHOW_DJ_PERMISSIONS.issubset(dj_permissions)
    assert "custom.permission" in dj_permissions
    assert producer_permissions == _LEGACY_SHOW_PRODUCER_PERMISSIONS


def test_schema_v6_upgrade_applies_legacy_rbac_seed(tmp_path, monkeypatch):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    _create_v6_db(
        db_path,
        """
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (4, 'viewer-1', 'Viewer 1', 'hash', 'viewer', 1);
        INSERT INTO stations (id, name) VALUES (1, 'Station 1');
        INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 2, 'dj');
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 3, 'producer');
        """,
    )

    init_db()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version")
        version = int(cur.fetchone()[0])
        templates = _role_template_ids(conn)
        user_roles = {
            int(row["user_id"]): int(row["role_template_id"])
            for row in cur.execute(
                "SELECT user_id, role_template_id FROM user_role_assignments"
            ).fetchall()
        }
        dj_permissions = {
            str(row["permission_key"])
            for row in cur.execute(
                "SELECT permission_key FROM show_assignment_permissions "
                "WHERE show_id = 1 AND user_id = 2"
            ).fetchall()
        }
    finally:
        conn.close()

    assert version == _SCHEMA_VERSION
    assert set(templates) == {
        "Legacy Admin",
        "Legacy DJ",
        "Legacy Producer",
        "Legacy Viewer",
    }
    assert user_roles == {
        1: templates["Legacy Admin"],
        2: templates["Legacy DJ"],
        3: templates["Legacy Producer"],
        4: templates["Legacy Viewer"],
    }
    assert dj_permissions == _LEGACY_SHOW_DJ_PERMISSIONS


def test_schema_v6_with_legacy_marker_heals_inactive_legacy_templates(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    _create_v6_db(
        db_path,
        """
        INSERT INTO system_settings (key, value)
            VALUES ('__legacy_rbac_seeded__', '1');
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (1, 'admin', 'Administrator', 'hash', 'admin', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (2, 'dj-1', 'DJ 1', 'hash', 'dj', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (3, 'producer-1', 'Producer 1', 'hash', 'producer', 1);
        INSERT INTO users (id, username, display_name, password_hash, role, is_active)
            VALUES (4, 'viewer-1', 'Viewer 1', 'hash', 'viewer', 1);
        INSERT INTO stations (id, name) VALUES (1, 'Station 1');
        INSERT INTO shows (id, station_id, name, is_active) VALUES (1, 1, 'Morning Show', 1);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (1, 'Legacy Admin', 'Legacy Admin compatibility template', 1, 0);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (2, 'Legacy DJ', 'Legacy DJ compatibility template', 1, 0);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (3, 'Legacy Producer', 'Legacy Producer compatibility template', 1, 0);
        INSERT INTO role_templates (id, name, description, is_system, is_active)
            VALUES (4, 'Legacy Viewer', 'Legacy Viewer compatibility template', 1, 0);
        INSERT INTO role_template_permissions (role_template_id, permission_key)
            VALUES (1, 'library.view');
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (1, 1);
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (2, 2);
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (3, 3);
        INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (4, 4);
        INSERT INTO show_assignments (show_id, user_id, role) VALUES (1, 2, 'dj');
        INSERT INTO show_assignment_permissions (show_id, user_id, permission_key)
            VALUES (1, 2, 'show.broadcast');
        """,
    )

    init_db()

    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        templates = _role_template_ids(conn)
        admin_effective_permissions = repo.list_effective_global_permissions(1)
        admin_roles = repo.list_user_role_ids(1)
        template_updated_at = {
            str(row["name"]): str(row["updated_at"])
            for row in conn.execute(
                "SELECT name, updated_at FROM role_templates ORDER BY id"
            ).fetchall()
        }
    finally:
        conn.close()

    assert templates["Legacy Admin"] == 1
    assert admin_roles == {templates["Legacy Admin"]}
    assert admin_effective_permissions == set(GLOBAL_PERMISSION_KEYS)

    time.sleep(1.1)
    init_db()

    conn = get_connection()
    try:
        second_template_updated_at = {
            str(row["name"]): str(row["updated_at"])
            for row in conn.execute(
                "SELECT name, updated_at FROM role_templates ORDER BY id"
            ).fetchall()
        }
    finally:
        conn.close()

    assert second_template_updated_at == template_updated_at


def test_custom_legacy_prefixed_role_does_not_trigger_system_template_reseed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "rbac.sqlite3"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    init_db()

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO role_templates (name, description, is_system, is_active) "
            "VALUES (?, ?, 0, 1)",
            ("Legacy Custom", "User managed role"),
        )
        conn.commit()
        template_updated_at = {
            str(row["name"]): str(row["updated_at"])
            for row in conn.execute(
                "SELECT name, updated_at FROM role_templates "
                "WHERE name IN ('Legacy Admin', 'Legacy DJ', 'Legacy Producer', 'Legacy Viewer') "
                "ORDER BY id"
            ).fetchall()
        }
    finally:
        conn.close()

    time.sleep(1.1)
    init_db()

    conn = get_connection()
    try:
        second_template_updated_at = {
            str(row["name"]): str(row["updated_at"])
            for row in conn.execute(
                "SELECT name, updated_at FROM role_templates "
                "WHERE name IN ('Legacy Admin', 'Legacy DJ', 'Legacy Producer', 'Legacy Viewer') "
                "ORDER BY id"
            ).fetchall()
        }
    finally:
        conn.close()

    assert second_template_updated_at == template_updated_at
