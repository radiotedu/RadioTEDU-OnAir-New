import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_data_root, get_db_path
from app.auth.permissions import GLOBAL_PERMISSION_KEYS, SHOW_PERMISSION_KEYS

_SCHEMA_VERSION = 22
_INIT_LOCK = threading.Lock()
_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: dict[str, object] = {"checked_at": 0.0, "path": "", "value": {}}
_SCHEMA_BOOTSTRAP_KEY = "__schema_bootstrapped__"
_LEGACY_RBAC_MIGRATION_KEY = "__legacy_rbac_seeded__"
_RBAC_INTEGRITY_REPAIR_KEY = "__rbac_integrity_repair_v17__"
_SCHEMA_BACKUP_DIRECTORY = "schema-backups"
_SCHEMA_BACKUP_LEDGER = "schema-migration-backups.json"
_DEFAULT_SCHEMA_BACKUP_RETENTION = 8

_LEGACY_ROLE_TEMPLATE_PERMISSIONS = {
    "Legacy Admin": set(GLOBAL_PERMISSION_KEYS),
    "Legacy DJ": {
        "stations.view",
        "library.view",
        "library.edit",
        "playlists.view",
        "playlists.edit",
        "schedule.view",
        "schedule.edit",
        "queue.view",
        "queue.edit",
        "soundboard.view",
        "soundboard.play",
        "program.panel.open",
        "shows.view",
        "logs.view",
        "stream.configure_basic",
    },
    "Legacy Producer": {
        "library.view",
        "library.edit",
        "playlists.view",
        "playlists.edit",
        "ads.view",
        "ads.edit",
        "schedule.view",
        "schedule.edit",
        "queue.view",
        "queue.edit",
        "program.panel.open",
        "shows.view",
        "logs.view",
    },
    "Legacy Viewer": {
        "stations.view",
        "library.view",
        "playlists.view",
        "ads.view",
        "schedule.view",
        "queue.view",
        "shows.view",
        "logs.view",
    },
}
_LEGACY_ROLE_TEMPLATE_NAMES = tuple(_LEGACY_ROLE_TEMPLATE_PERMISSIONS)

_LEGACY_ROLE_TO_TEMPLATE = {
    "admin": "Legacy Admin",
    "dj": "Legacy DJ",
    "producer": "Legacy Producer",
    "viewer": "Legacy Viewer",
}

_LEGACY_SHOW_ROLE_PERMISSIONS = {
    "dj": {
        "show.broadcast",
        "show.queue_edit",
        "show.jingle_manage",
        "show.break_control",
        "show.end",
    },
    "producer": {
        "show.queue_edit",
        "show.jingle_manage",
        "show.break_control",
    },
}


def _ensure_default_admin(cur) -> None:
    from app.auth.password import hash_password

    cur.execute("SELECT id FROM users WHERE username='admin'")
    if cur.fetchone() is not None:
        return

    configured_password = os.getenv("CLEANROOM_INITIAL_ADMIN_PASSWORD", "").strip()
    initial_password = configured_password or secrets.token_urlsafe(24)
    cur.execute(
        "INSERT INTO users (username, display_name, password_hash, role, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        ("admin", "Administrator", hash_password(initial_password), "admin", 1),
    )

    if not configured_password:
        credential_path = get_data_root() / "initial-admin-password.txt"
        credential_path.parent.mkdir(parents=True, exist_ok=True)
        credential_path.write_text(
            "RadioTEDU OnAir initial administrator credentials\n"
            "Username: admin\n"
            f"Password: {initial_password}\n"
            "Sign in locally and change this password immediately.\n",
            encoding="utf-8",
        )
        try:
            credential_path.chmod(0o600)
        except OSError:
            pass


def _ensure_default_station(cur) -> None:
    cur.execute("SELECT id FROM stations ORDER BY id ASC LIMIT 1")
    row = cur.fetchone()
    if row is not None:
        station_id = int(row[0])
    else:
        cur.execute("INSERT INTO stations (name) VALUES (?)", ("Main Radio",))
        station_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO NOTHING",
        ("active_station_id", str(station_id)),
    )
    cur.execute(
        "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO NOTHING",
        ("speaker_monitor_station_id", str(station_id)),
    )


def _legacy_rbac_migration_applied(cur) -> bool:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'"
    )
    if cur.fetchone() is None:
        return False
    cur.execute(
        "SELECT 1 FROM system_settings WHERE key = ? LIMIT 1",
        (_LEGACY_RBAC_MIGRATION_KEY,),
    )
    return cur.fetchone() is not None


def _schema_bootstrap_applied(cur) -> bool:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'"
    )
    if cur.fetchone() is None:
        return False
    cur.execute(
        "SELECT 1 FROM system_settings WHERE key = ? AND value = ? LIMIT 1",
        (_SCHEMA_BOOTSTRAP_KEY, str(_SCHEMA_VERSION)),
    )
    return cur.fetchone() is not None


def _post_version_repairs_needed(cur) -> bool:
    """Detect additive repairs introduced after a schema version shipped.

    Version 20 installations can predate delivered-variant accounting.  The
    repair is additive and preserves every append-only usage row, but it must
    still run even when the version/bootstrap markers already match.
    """
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='music_usage_log'"
    )
    if cur.fetchone() is None:
        return True
    columns = {
        str(row[1])
        for row in cur.execute("PRAGMA table_info(music_usage_log)").fetchall()
    }
    if "delivered_variants_json" not in columns:
        return True
    return False


def _mark_schema_bootstrap_applied(cur) -> None:
    cur.execute(
        "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (_SCHEMA_BOOTSTRAP_KEY, str(_SCHEMA_VERSION)),
    )


def _mark_legacy_rbac_migration_applied(cur) -> None:
    cur.execute(
        "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
        (_LEGACY_RBAC_MIGRATION_KEY, "1"),
    )


def _repair_rbac_foreign_key_integrity(cur) -> int:
    """Remove only orphaned RBAC joins and reject unrelated corruption."""
    allowed_parents = {"users", "role_templates"}
    unexpected: set[str] = set()
    allowed_count = 0
    for row in cur.execute("PRAGMA foreign_key_check"):
        table_name = str(row[0])
        parent_name = str(row[2])
        if table_name == "user_role_assignments" and parent_name in allowed_parents:
            allowed_count += 1
            continue
        unexpected.add(f"{table_name}->{parent_name}")

    if unexpected:
        summary = ", ".join(sorted(unexpected)[:8])
        if len(unexpected) > 8:
            summary += f", +{len(unexpected) - 8} more"
        raise RuntimeError(
            "schema migration blocked by unexpected foreign-key violations: " + summary
        )

    removed_count = 0
    if allowed_count:
        cur.execute(
            "DELETE FROM user_role_assignments "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM users WHERE users.id = user_role_assignments.user_id"
            ") OR NOT EXISTS ("
            "SELECT 1 FROM role_templates "
            "WHERE role_templates.id = user_role_assignments.role_template_id"
            ")"
        )
        removed_count = max(0, int(cur.rowcount))

    remaining = cur.execute("PRAGMA foreign_key_check").fetchone()
    if remaining is not None:
        raise RuntimeError(
            "schema migration failed to restore foreign-key integrity: "
            f"{remaining[0]}->{remaining[2]}"
        )

    cur.execute(
        "INSERT INTO system_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (
            _RBAC_INTEGRITY_REPAIR_KEY,
            json.dumps(
                {"removed_assignments": removed_count, "schema_version": 17},
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    return removed_count


def _legacy_role_template_ids(cur) -> dict[str, int]:
    template_ids: dict[str, int] = {}
    placeholders = ", ".join("?" for _ in _LEGACY_ROLE_TEMPLATE_NAMES)
    cur.execute(
        f"SELECT id, name FROM role_templates WHERE name IN ({placeholders})",
        _LEGACY_ROLE_TEMPLATE_NAMES,
    )
    for row in cur.fetchall():
        template_ids[str(row[1])] = int(row[0])
    return template_ids


def _legacy_role_templates_need_sync(cur) -> bool:
    template_ids = _legacy_role_template_ids(cur)
    if set(template_ids) != set(_LEGACY_ROLE_TEMPLATE_PERMISSIONS):
        return True

    cur.execute(
        "SELECT name, description, is_system, is_active FROM role_templates "
        f"WHERE name IN ({', '.join('?' for _ in _LEGACY_ROLE_TEMPLATE_NAMES)})"
        ,
        _LEGACY_ROLE_TEMPLATE_NAMES,
    )
    expected_descriptions = {
        name: f"{name} compatibility template"
        for name in _LEGACY_ROLE_TEMPLATE_PERMISSIONS
    }
    for row in cur.fetchall():
        name = str(row[0])
        if str(row[1]) != expected_descriptions[name]:
            return True
        if int(row[2]) != 1 or int(row[3]) != 1:
            return True

    for name, permission_keys in _LEGACY_ROLE_TEMPLATE_PERMISSIONS.items():
        role_template_id = template_ids[name]
        cur.execute(
            "SELECT permission_key FROM role_template_permissions "
            "WHERE role_template_id = ?",
            (role_template_id,),
        )
        current_permissions = {str(row[0]) for row in cur.fetchall()}
        if current_permissions != permission_keys:
            return True
    return False


def _legacy_user_role_assignments_need_sync(cur) -> bool:
    template_ids = _legacy_role_template_ids(cur)
    if len(template_ids) != len(_LEGACY_ROLE_TO_TEMPLATE):
        return True

    cur.execute(
        "SELECT id, role FROM users WHERE role IN (?, ?, ?, ?)",
        ("admin", "dj", "producer", "viewer"),
    )
    for row in cur.fetchall():
        user_id = int(row[0])
        role_name = str(row[1])
        template_name = _LEGACY_ROLE_TO_TEMPLATE.get(role_name)
        if template_name is None:
            continue
        role_template_id = template_ids.get(template_name)
        if role_template_id is None:
            return True
        cur.execute(
            "SELECT 1 FROM user_role_assignments "
            "WHERE user_id = ? AND role_template_id = ? LIMIT 1",
            (user_id, role_template_id),
        )
        if cur.fetchone() is None:
            return True
    return False


def _legacy_show_assignment_permissions_need_sync(cur) -> bool:
    cur.execute(
        "SELECT show_id, user_id, role, COALESCE(permission_keys_json, '') AS permission_keys_json "
        "FROM show_assignments "
        "WHERE role IN ('dj', 'producer')"
    )
    for row in cur.fetchall():
        show_id = int(row[0])
        user_id = int(row[1])
        role_name = str(row[2])
        permission_keys_json = str(row[3] or "")
        if permission_keys_json:
            continue
        expected_permissions = _LEGACY_SHOW_ROLE_PERMISSIONS.get(role_name)
        if not expected_permissions:
            continue
        cur.execute(
            "SELECT permission_key FROM show_assignment_permissions "
            "WHERE show_id = ? AND user_id = ?",
            (show_id, user_id),
        )
        current_permissions = {str(permission_row[0]) for permission_row in cur.fetchall()}
        if not expected_permissions.issubset(current_permissions):
            return True
    return False


def _legacy_rbac_needs_sync(cur) -> bool:
    if not _legacy_rbac_migration_applied(cur):
        return True
    if _legacy_role_templates_need_sync(cur):
        return True
    if _legacy_user_role_assignments_need_sync(cur):
        return True
    if _legacy_show_assignment_permissions_need_sync(cur):
        return True
    return False


def _sync_legacy_rbac(cur) -> None:
    # Keep this idempotent so older v6 databases can self-heal if the legacy
    # templates were left inactive or partially seeded.
    _seed_legacy_role_templates(cur)
    _backfill_user_role_assignments(cur)
    _backfill_show_assignment_permissions(cur)
    _mark_legacy_rbac_migration_applied(cur)


def _seed_legacy_role_templates(cur) -> None:
    for name, permission_keys in _LEGACY_ROLE_TEMPLATE_PERMISSIONS.items():
        cur.execute(
            "INSERT OR IGNORE INTO role_templates (name, description, is_system, is_active) "
            "VALUES (?, ?, 1, 1)",
            (name, f"{name} compatibility template"),
        )
        cur.execute(
            "UPDATE role_templates "
            "SET description = ?, is_system = 1, is_active = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE name = ?",
            (f"{name} compatibility template", name),
        )
        cur.execute("SELECT id FROM role_templates WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            continue
        role_template_id = int(row[0])
        cur.execute(
            "DELETE FROM role_template_permissions WHERE role_template_id = ?",
            (role_template_id,),
        )
        cur.executemany(
            "INSERT INTO role_template_permissions (role_template_id, permission_key) "
            "VALUES (?, ?)",
            [(role_template_id, permission_key) for permission_key in sorted(permission_keys)],
        )


def _backfill_user_role_assignments(cur) -> None:
    template_ids = {}
    for role_name, template_name in _LEGACY_ROLE_TO_TEMPLATE.items():
        cur.execute("SELECT id FROM role_templates WHERE name = ?", (template_name,))
        row = cur.fetchone()
        if row is not None:
            template_ids[role_name] = int(row[0])

    if not template_ids:
        return

    cur.execute(
        "SELECT id, role FROM users WHERE role IN (?, ?, ?, ?)",
        ("admin", "dj", "producer", "viewer"),
    )
    rows = cur.fetchall()
    for row in rows:
        template_id = template_ids.get(str(row[1]))
        if template_id is None:
            continue
        cur.execute(
            "INSERT OR IGNORE INTO user_role_assignments (user_id, role_template_id) VALUES (?, ?)",
            (int(row[0]), template_id),
        )


def _backfill_show_assignment_permissions(cur) -> None:
    cur.execute(
        "SELECT sa.show_id, sa.user_id, sa.role, COALESCE(sa.permission_keys_json, '') AS permission_keys_json "
        "FROM show_assignments sa "
        "WHERE sa.role IN ('dj', 'producer')"
    )
    rows = cur.fetchall()
    for row in rows:
        if str(row[3] or ""):
            continue
        permission_keys = _LEGACY_SHOW_ROLE_PERMISSIONS.get(str(row[2]))
        if not permission_keys:
            continue
        cur.executemany(
            "INSERT OR IGNORE INTO show_assignment_permissions (show_id, user_id, permission_key) "
            "VALUES (?, ?, ?)",
            [
                (int(row[0]), int(row[1]), permission_key)
                for permission_key in sorted(permission_keys)
                if permission_key in SHOW_PERMISSION_KEYS
            ],
        )


def _migrate_queue_items(cur) -> None:
    cur.execute("PRAGMA table_info(queue_items)")
    existing = {row[1] for row in cur.fetchall()}
    if "status" not in existing:
        cur.execute(
            "ALTER TABLE queue_items ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "enqueued_at" not in existing:
        cur.execute("ALTER TABLE queue_items ADD COLUMN enqueued_at TEXT")
    if "started_at" not in existing:
        cur.execute("ALTER TABLE queue_items ADD COLUMN started_at TEXT")
    if "finished_at" not in existing:
        cur.execute("ALTER TABLE queue_items ADD COLUMN finished_at TEXT")
    if "dedupe_key" not in existing:
        cur.execute("ALTER TABLE queue_items ADD COLUMN dedupe_key TEXT")
    cur.execute("UPDATE queue_items SET status='pending' WHERE status IS NULL")
    cur.execute(
        "UPDATE queue_items SET enqueued_at=CURRENT_TIMESTAMP WHERE enqueued_at IS NULL"
    )
    # Only one active automatic command may own a station/dedupe key. Older
    # builds allowed duplicate guards to enqueue the same jingle or recovery
    # item. Preserve the playing/oldest row and terminally quarantine extras
    # before creating the concurrency barrier below.
    cur.execute(
        "WITH ranked AS ("
        "SELECT id, ROW_NUMBER() OVER ("
        "PARTITION BY station_id, dedupe_key "
        "ORDER BY CASE status WHEN 'playing' THEN 0 ELSE 1 END, id"
        ") AS ordinal "
        "FROM queue_items "
        "WHERE status IN ('pending', 'playing') "
        "AND dedupe_key IS NOT NULL AND TRIM(dedupe_key) <> ''"
        ") "
        "UPDATE queue_items SET status='failed', "
        "finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP) "
        "WHERE id IN (SELECT id FROM ranked WHERE ordinal > 1)"
    )


def _migrate_schedule_items(cur) -> None:
    cur.execute("PRAGMA table_info(schedule_items)")
    existing = {row[1] for row in cur.fetchall()}
    if "event_name" not in existing:
        cur.execute(
            "ALTER TABLE schedule_items ADD COLUMN event_name TEXT NOT NULL DEFAULT ''"
        )


def _migrate_ad_break_items(cur) -> None:
    cur.execute("PRAGMA table_info(ad_break_items)")
    existing = {row[1] for row in cur.fetchall()}
    if "started_at" not in existing:
        cur.execute("ALTER TABLE ad_break_items ADD COLUMN started_at TEXT")
    if "finished_at" not in existing:
        cur.execute("ALTER TABLE ad_break_items ADD COLUMN finished_at TEXT")
    if "dedupe_key" not in existing:
        cur.execute("ALTER TABLE ad_break_items ADD COLUMN dedupe_key TEXT")


def _migrate_station_outputs(cur) -> None:
    cur.execute("PRAGMA table_info(station_outputs)")
    existing = {row[1] for row in cur.fetchall()}
    if "icecast_enabled" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "icecast_host" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_host TEXT NOT NULL DEFAULT '127.0.0.1'"
        )
    if "icecast_port" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_port INTEGER NOT NULL DEFAULT 8000"
        )
    if "icecast_mount" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_mount TEXT NOT NULL DEFAULT '/stream'"
        )
    if "icecast_user" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_user TEXT NOT NULL DEFAULT 'source'"
        )
    if "icecast_password" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN icecast_password TEXT NOT NULL DEFAULT ''"
        )
    if "output_gain_db" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN output_gain_db REAL NOT NULL DEFAULT 0"
        )
    if "stream_codec_profile" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN stream_codec_profile TEXT NOT NULL DEFAULT 'he_aac_192'"
        )
    if "stream_bitrate_kbps" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN stream_bitrate_kbps INTEGER NOT NULL DEFAULT 192"
        )
    if "source_protocol" not in existing:
        cur.execute(
            "ALTER TABLE station_outputs ADD COLUMN source_protocol TEXT NOT NULL DEFAULT 'icecast'"
        )
    cur.execute(
        "UPDATE station_outputs SET source_protocol='icecast' "
        "WHERE source_protocol IS NULL OR TRIM(source_protocol)='' "
        "OR LOWER(source_protocol) NOT IN ('icecast','shoutcast')"
    )


def _migrate_station_credentials(cur) -> None:
    """Move legacy Icecast passwords out of SQLite before schema v9 commits."""
    from app.security.credential_vault import (
        is_credential_reference,
        store_station_icecast_password,
        store_system_secret,
    )

    cur.execute(
        "SELECT station_id, icecast_password FROM station_outputs ORDER BY station_id"
    )
    output_passwords = {
        int(row[0]): str(row[1] or "")
        for row in cur.fetchall()
    }
    cur.execute(
        "SELECT station_id, value FROM station_settings "
        "WHERE key='icecast_password' ORDER BY station_id"
    )
    settings_passwords = {
        int(row[0]): str(row[1] or "")
        for row in cur.fetchall()
    }

    for station_id in sorted(set(output_passwords) | set(settings_passwords)):
        stored_value = output_passwords.get(station_id, "")
        legacy_value = settings_passwords.get(station_id, "")
        if is_credential_reference(stored_value):
            reference = stored_value
        else:
            plaintext = stored_value or legacy_value
            if plaintext.strip().lower() == ("hack" + "me"):
                plaintext = ""
            reference = (
                store_station_icecast_password(station_id, plaintext)
                if plaintext
                else ""
            )

        if station_id in output_passwords:
            cur.execute(
                "UPDATE station_outputs SET icecast_password=? WHERE station_id=?",
                (reference, station_id),
            )
        elif reference:
            cur.execute(
                "INSERT INTO station_outputs "
                "(station_id, icecast_enabled, icecast_host, icecast_port, "
                "icecast_mount, icecast_user, icecast_password) "
                "VALUES (?, 1, '127.0.0.1', 8000, '/stream', 'source', ?)",
                (station_id, reference),
            )

    cur.execute(
        "UPDATE station_settings SET value='' "
        "WHERE key='icecast_password' AND value<>''"
    )

    for key in (
        "rocket_admin_password",
        "rocket_health_password",
        "radiotedu_voting_agent_token",
    ):
        cur.execute("SELECT value FROM system_settings WHERE key=?", (key,))
        row = cur.fetchone()
        stored_value = str(row[0] or "") if row is not None else ""
        if stored_value and not is_credential_reference(stored_value):
            cur.execute(
                "UPDATE system_settings SET value=? WHERE key=?",
                (store_system_secret(key, stored_value), key),
            )


def _migrate_tracks(cur) -> None:
    cur.execute("PRAGMA table_info(tracks)")
    existing = {row[1] for row in cur.fetchall()}
    if "file_path" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN file_path TEXT DEFAULT ''")
    if "station_id" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN station_id INTEGER NOT NULL DEFAULT 1")
    if "track_type" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN track_type TEXT NOT NULL DEFAULT 'music'")
    if "album" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN album TEXT NOT NULL DEFAULT ''")
    if "genre" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN genre TEXT NOT NULL DEFAULT ''")
    if "language" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN language TEXT NOT NULL DEFAULT ''")
    if "duration" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN duration REAL NOT NULL DEFAULT 0")
    if "bpm" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN bpm REAL NOT NULL DEFAULT 0")
    if "is_active" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "exclude_from_autoplay" not in existing:
        cur.execute(
            "ALTER TABLE tracks ADD COLUMN exclude_from_autoplay INTEGER NOT NULL DEFAULT 0"
        )
    if "play_count" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
    if "last_played_at" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN last_played_at TEXT")
    if "managed_file_size" not in existing:
        cur.execute(
            "ALTER TABLE tracks ADD COLUMN managed_file_size INTEGER NOT NULL DEFAULT -1"
        )
    if "managed_file_mtime_ns" not in existing:
        cur.execute(
            "ALTER TABLE tracks ADD COLUMN managed_file_mtime_ns INTEGER NOT NULL DEFAULT -1"
        )
    if "cover_art_url" not in existing:
        cur.execute("ALTER TABLE tracks ADD COLUMN cover_art_url TEXT NOT NULL DEFAULT ''")


def _migrate_daypart_rules(cur) -> None:
    """Upgrade the original Monday-only clock without losing operator edits."""
    cur.execute("PRAGMA table_info(daypart_rules)")
    existing = {row[1] for row in cur.fetchall()}
    if not existing or "day_of_week" in existing:
        return
    cur.execute("ALTER TABLE daypart_rules RENAME TO daypart_rules_daily")
    cur.execute("DROP INDEX IF EXISTS idx_daypart_rules_station_position")
    cur.execute(
        "CREATE TABLE daypart_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER NOT NULL, "
        "day_of_week INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0, "
        "name TEXT NOT NULL, start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL, "
        "min_bpm REAL NOT NULL, max_bpm REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(station_id, day_of_week, position), "
        "FOREIGN KEY(station_id) REFERENCES stations(id) ON DELETE CASCADE, "
        "CHECK(day_of_week >= 0 AND day_of_week <= 6), "
        "CHECK(start_minute >= 0 AND start_minute < 1440), "
        "CHECK(end_minute >= 0 AND end_minute < 1440), "
        "CHECK(start_minute <> end_minute), "
        "CHECK(min_bpm >= 30 AND max_bpm <= 240 AND min_bpm <= max_bpm)"
        ")"
    )
    cur.execute(
        "INSERT INTO daypart_rules "
        "(id, station_id, day_of_week, position, name, start_minute, end_minute, "
        "min_bpm, max_bpm, enabled, created_at, updated_at) "
        "SELECT id, station_id, 0, position, name, start_minute, end_minute, "
        "min_bpm, max_bpm, enabled, created_at, updated_at FROM daypart_rules_daily"
    )
    cur.execute("DROP TABLE daypart_rules_daily")


def _migrate_show_assignment_permissions(cur) -> None:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='show_assignment_permissions'"
    )
    exists = cur.fetchone() is not None
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='show_assignment_permissions_old'"
    )
    old_exists = cur.fetchone() is not None

    if old_exists:
        if exists:
            cur.execute("DROP TABLE show_assignment_permissions")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS show_assignment_permissions ("
            "show_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "permission_key TEXT NOT NULL, "
            "PRIMARY KEY(show_id, user_id, permission_key), "
            "FOREIGN KEY(show_id, user_id) REFERENCES show_assignments(show_id, user_id) ON DELETE CASCADE"
            ")"
        )
        cur.execute(
            "INSERT INTO show_assignment_permissions (show_id, user_id, permission_key) "
            "SELECT p.show_id, p.user_id, p.permission_key "
            "FROM show_assignment_permissions_old p "
            "WHERE EXISTS ("
            "SELECT 1 FROM show_assignments a "
            "WHERE a.show_id = p.show_id AND a.user_id = p.user_id"
            ")"
        )
        cur.execute("DROP TABLE show_assignment_permissions_old")
        return

    if not exists:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS show_assignment_permissions ("
            "show_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "permission_key TEXT NOT NULL, "
            "PRIMARY KEY(show_id, user_id, permission_key), "
            "FOREIGN KEY(show_id, user_id) REFERENCES show_assignments(show_id, user_id) ON DELETE CASCADE"
            ")"
        )
        return

    cur.execute("PRAGMA foreign_key_list(show_assignment_permissions)")
    fk_rows = cur.fetchall()
    current = {(row[3], row[4], row[6]) for row in fk_rows}
    expected = {
        ("show_id", "show_id", "CASCADE"),
        ("user_id", "user_id", "CASCADE"),
    }
    if current == expected:
        return

    cur.execute("DROP TABLE IF EXISTS show_assignment_permissions_old")
    cur.execute("ALTER TABLE show_assignment_permissions RENAME TO show_assignment_permissions_old")
    cur.execute(
        "CREATE TABLE show_assignment_permissions ("
        "show_id INTEGER NOT NULL, "
        "user_id INTEGER NOT NULL, "
        "permission_key TEXT NOT NULL, "
        "PRIMARY KEY(show_id, user_id, permission_key), "
        "FOREIGN KEY(show_id, user_id) REFERENCES show_assignments(show_id, user_id) ON DELETE CASCADE"
        ")"
    )
    cur.execute(
        "INSERT INTO show_assignment_permissions (show_id, user_id, permission_key) "
        "SELECT p.show_id, p.user_id, p.permission_key "
        "FROM show_assignment_permissions_old p "
        "WHERE EXISTS ("
        "SELECT 1 FROM show_assignments a "
        "WHERE a.show_id = p.show_id AND a.user_id = p.user_id"
        ")"
    )
    cur.execute("DROP TABLE show_assignment_permissions_old")


def _migrate_show_assignments(cur) -> None:
    cur.execute("PRAGMA table_info(show_assignments)")
    existing = {row[1] for row in cur.fetchall()}
    if "permission_keys_json" not in existing:
        cur.execute(
            "ALTER TABLE show_assignments ADD COLUMN permission_keys_json TEXT NOT NULL DEFAULT ''"
        )


def _migrate_playlists(cur) -> None:
    cur.execute("PRAGMA table_info(playlists)")
    existing = {row[1] for row in cur.fetchall()}
    if "description" not in existing:
        cur.execute(
            "ALTER TABLE playlists ADD COLUMN description TEXT NOT NULL DEFAULT ''"
        )
    if "playlist_type" not in existing:
        cur.execute(
            "ALTER TABLE playlists ADD COLUMN playlist_type TEXT NOT NULL DEFAULT 'manual'"
        )


def _migrate_ad_break_sets(cur) -> None:
    cur.execute("PRAGMA table_info(ad_break_sets)")
    existing = {row[1] for row in cur.fetchall()}
    if "payload_json" not in existing:
        cur.execute(
            "ALTER TABLE ad_break_sets ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
        )


def _migrate_studios(cur) -> None:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='studios'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS studios ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE, "
            "name TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "sort_order INTEGER NOT NULL DEFAULT 1, "
            "is_active INTEGER NOT NULL DEFAULT 1, "
            "is_on_air INTEGER NOT NULL DEFAULT 0, "
            "current_user_id INTEGER REFERENCES users(id), "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studios_station_sort ON studios(station_id, sort_order, id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studios_station_on_air ON studios(station_id, is_on_air)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studios_station_active ON studios(station_id, is_active)"
        )
        return

    cur.execute("PRAGMA table_info(studios)")
    existing = {row[1] for row in cur.fetchall()}
    if "description" not in existing:
        cur.execute("ALTER TABLE studios ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "sort_order" not in existing:
        cur.execute("ALTER TABLE studios ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 1")
    if "is_active" not in existing:
        cur.execute("ALTER TABLE studios ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "is_on_air" not in existing:
        cur.execute("ALTER TABLE studios ADD COLUMN is_on_air INTEGER NOT NULL DEFAULT 0")
    if "current_user_id" not in existing:
        cur.execute("ALTER TABLE studios ADD COLUMN current_user_id INTEGER REFERENCES users(id)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studios_station_sort ON studios(station_id, sort_order, id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studios_station_on_air ON studios(station_id, is_on_air)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studios_station_active ON studios(station_id, is_active)"
    )


def _migrate_studio_sessions(cur) -> None:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='studio_sessions'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS studio_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "session_role TEXT NOT NULL DEFAULT 'dj', "
            "status TEXT NOT NULL DEFAULT 'active', "
            "joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "left_at TEXT, "
            "last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE(studio_id, user_id)"
            ")"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studio_sessions_studio_status ON studio_sessions(studio_id, status, id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studio_sessions_user_status ON studio_sessions(user_id, status, id)"
        )
        return

    cur.execute("PRAGMA table_info(studio_sessions)")
    existing = {row[1] for row in cur.fetchall()}
    if "session_role" not in existing:
        cur.execute(
            "ALTER TABLE studio_sessions ADD COLUMN session_role TEXT NOT NULL DEFAULT 'dj'"
        )
    if "status" not in existing:
        cur.execute(
            "ALTER TABLE studio_sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    if "joined_at" not in existing:
        cur.execute(
            "ALTER TABLE studio_sessions ADD COLUMN joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    if "left_at" not in existing:
        cur.execute("ALTER TABLE studio_sessions ADD COLUMN left_at TEXT")
    if "last_seen_at" not in existing:
        cur.execute(
            "ALTER TABLE studio_sessions ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studio_sessions_studio_status ON studio_sessions(studio_id, status, id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studio_sessions_user_status ON studio_sessions(user_id, status, id)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_studio_sessions_studio_user_unique ON studio_sessions(studio_id, user_id)"
    )


def _migrate_studio_chat_messages(cur) -> None:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='studio_chat_messages'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS studio_chat_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "message TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_studio_chat_messages_studio_created ON studio_chat_messages(studio_id, created_at DESC, id DESC)"
        )
        return

    cur.execute("PRAGMA table_info(studio_chat_messages)")
    existing = {row[1] for row in cur.fetchall()}
    if "message" not in existing:
        cur.execute("ALTER TABLE studio_chat_messages ADD COLUMN message TEXT NOT NULL DEFAULT ''")
    if "created_at" not in existing:
        cur.execute(
            "ALTER TABLE studio_chat_messages ADD COLUMN created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_studio_chat_messages_studio_created ON studio_chat_messages(studio_id, created_at DESC, id DESC)"
    )


def _backfill_default_studios(cur) -> None:
    cur.execute("SELECT id FROM stations ORDER BY id ASC")
    station_ids = [int(row[0]) for row in cur.fetchall()]
    for station_id in station_ids:
        cur.execute(
            "SELECT 1 FROM studios WHERE station_id=? LIMIT 1",
            (station_id,),
        )
        if cur.fetchone() is not None:
            continue
        cur.execute(
            "INSERT INTO studios (station_id, name, description, sort_order, is_active, is_on_air) "
            "VALUES (?, 'Studio A', '', 1, 1, 1)",
            (station_id,),
        )


def _schema_backup_directory(db_path: Path) -> Path:
    return db_path.parent / _SCHEMA_BACKUP_DIRECTORY


def _schema_backup_ledger_path(db_path: Path) -> Path:
    return db_path.parent / _SCHEMA_BACKUP_LEDGER


def _schema_backup_retention() -> int:
    raw = os.getenv(
        "RADIOTEDU_SCHEMA_BACKUP_RETENTION",
        str(_DEFAULT_SCHEMA_BACKUP_RETENTION),
    )
    try:
        return max(1, min(64, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_SCHEMA_BACKUP_RETENTION


def _fsync_path(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/fsync.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync; Windows does not allow opening directories."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _read_schema_backup_ledger(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("schema backup ledger is unreadable") from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        raise RuntimeError("schema backup ledger is invalid")
    return [dict(record) for record in records]


def _write_schema_backup_ledger(path: Path, records: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    payload = json.dumps({"version": 1, "records": records}, indent=2, sort_keys=True)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_schema_backup(db_path: Path, backup_path: Path) -> None:
    ledger_path = _schema_backup_ledger_path(db_path)
    backup_directory = _schema_backup_directory(db_path).resolve()
    database_key = str(db_path.resolve())
    records = _read_schema_backup_ledger(ledger_path)
    records.append(
        {
            "backup_path": str(backup_path.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_path": database_key,
            "schema_version": _SCHEMA_VERSION,
            "size_bytes": int(backup_path.stat().st_size),
        }
    )

    matching = [record for record in records if record.get("database_path") == database_key]
    retained_matching = matching[-_schema_backup_retention() :]
    retained_ids = {id(record) for record in retained_matching}
    removed = [
        record
        for record in matching
        if id(record) not in retained_ids
    ]
    retained = [
        record
        for record in records
        if record.get("database_path") != database_key or id(record) in retained_ids
    ]
    _write_schema_backup_ledger(ledger_path, retained)

    for record in removed:
        candidate = Path(str(record.get("backup_path") or ""))
        try:
            if candidate.resolve().parent == backup_directory:
                candidate.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            # The durable ledger has already advanced. A later migration can retry
            # housekeeping without putting schema safety at risk.
            continue


def _backup_database_before_schema_migration(db_path: Path) -> Path:
    """Create an atomic, integrity-checked SQLite backup before schema writes."""
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise RuntimeError("refusing to back up a missing or empty existing database")

    backup_directory = _schema_backup_directory(db_path)
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_directory / (
        f"{db_path.stem}.before-schema-v{_SCHEMA_VERSION}-{timestamp}-{secrets.token_hex(4)}"
        f"{db_path.suffix or '.sqlite3'}"
    )
    temporary_path = backup_path.with_name(f".{backup_path.name}.tmp")
    source = None
    destination = None
    published = False
    try:
        source = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        source_check = source.execute("PRAGMA quick_check(1)").fetchone()
        if str(source_check[0] if source_check else "").lower() != "ok":
            raise sqlite3.DatabaseError("source database integrity check failed")

        destination = sqlite3.connect(str(temporary_path))
        source.backup(destination)
        destination.commit()
        backup_check = destination.execute("PRAGMA quick_check(1)").fetchone()
        if str(backup_check[0] if backup_check else "").lower() != "ok":
            raise sqlite3.DatabaseError("schema backup integrity check failed")
        destination.close()
        destination = None
        source.close()
        source = None

        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("schema backup was not written")
        _fsync_path(temporary_path)
        os.replace(temporary_path, backup_path)
        published = True
        _fsync_directory(backup_directory)
        _record_schema_backup(db_path, backup_path)
        return backup_path
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if not published:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def get_connection(*, timeout_seconds: float = 30.0):
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    safe_timeout = max(0.05, float(timeout_seconds))
    busy_timeout_ms = max(50, int(safe_timeout * 1000.0))
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=safe_timeout,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    try:
        journal_attempts = 6 if safe_timeout >= 5.0 else 1
        for attempt in range(journal_attempts):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= journal_attempts - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
        synchronous = str(
            os.getenv("RADIOTEDU_SQLITE_SYNCHRONOUS", "FULL") or "FULL"
        ).strip().upper()
        if synchronous not in {"NORMAL", "FULL", "EXTRA"}:
            synchronous = "FULL"
        conn.execute(f"PRAGMA synchronous={synchronous}")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except Exception:
        conn.close()
        raise


def database_health_snapshot(
    *,
    max_age_seconds: float = 15.0,
    force: bool = False,
) -> dict[str, object]:
    """Return a bounded, cached SQLite integrity and storage readiness snapshot."""
    now = time.monotonic()
    db_path = get_db_path()
    with _HEALTH_LOCK:
        cached_at = float(_HEALTH_CACHE.get("checked_at") or 0.0)
        cached_path = str(_HEALTH_CACHE.get("path") or "")
        cached_value = dict(_HEALTH_CACHE.get("value") or {})
        if (
            not force
            and cached_path == str(db_path)
            and cached_value
            and now - cached_at < max(0.0, max_age_seconds)
        ):
            return cached_value

        conn = None
        try:
            conn = get_connection()
            quick_check_row = conn.execute("PRAGMA quick_check(1)").fetchone()
            quick_check = str(quick_check_row[0] if quick_check_row else "")
            journal_mode_row = conn.execute("PRAGMA journal_mode").fetchone()
            synchronous_row = conn.execute("PRAGMA synchronous").fetchone()
            foreign_keys_row = conn.execute("PRAGMA foreign_keys").fetchone()
            page_count_row = conn.execute("PRAGMA page_count").fetchone()
            page_size_row = conn.execute("PRAGMA page_size").fetchone()

            usage = shutil.disk_usage(db_path.parent)
            free_percent = (
                (float(usage.free) / float(usage.total)) * 100.0
                if usage.total
                else 0.0
            )
            # Percentage-only critical thresholds misclassify large volumes:
            # 3.5 GiB free on a 1 TiB system disk is low percentage but still
            # ample headroom for this SQLite control database.  Keep it ready,
            # surface a degraded warning, and reserve critical for real
            # exhaustion where commits/WAL checkpoints are at imminent risk.
            disk_critical = usage.free < 512 * 1024 * 1024
            disk_warning = usage.free < 2 * 1024 * 1024 * 1024 or free_percent < 10.0
            synchronous_value = int(synchronous_row[0] if synchronous_row else 0)
            integrity_ok = quick_check.lower() == "ok"
            foreign_keys_enabled = bool(
                int(foreign_keys_row[0] if foreign_keys_row else 0)
            )
            journal_mode = str(
                journal_mode_row[0] if journal_mode_row else ""
            ).lower()
            healthy = (
                integrity_ok
                and foreign_keys_enabled
                and journal_mode == "wal"
                and synchronous_value >= 2
                and not disk_critical
            )
            state = "critical" if not integrity_ok or disk_critical else (
                "degraded" if not healthy or disk_warning else "operational"
            )
            value: dict[str, object] = {
                "state": state,
                "healthy": healthy,
                "integrity": quick_check,
                "journal_mode": journal_mode,
                "synchronous": {
                    0: "off",
                    1: "normal",
                    2: "full",
                    3: "extra",
                }.get(synchronous_value, f"unknown:{synchronous_value}"),
                "foreign_keys": foreign_keys_enabled,
                "database_bytes": (
                    int(db_path.stat().st_size) if db_path.exists() else 0
                ),
                "wal_bytes": (
                    int(db_path.with_name(db_path.name + "-wal").stat().st_size)
                    if db_path.with_name(db_path.name + "-wal").exists()
                    else 0
                ),
                "allocated_bytes": int(page_count_row[0] if page_count_row else 0)
                * int(page_size_row[0] if page_size_row else 0),
                "disk_free_bytes": int(usage.free),
                "disk_free_percent": round(free_percent, 2),
            }
        except Exception as exc:
            value = {
                "state": "critical",
                "healthy": False,
                "integrity": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if conn is not None:
                conn.close()

        _HEALTH_CACHE["checked_at"] = now
        _HEALTH_CACHE["path"] = str(db_path)
        _HEALTH_CACHE["value"] = dict(value)
        return dict(value)


def _bootstrap_schema(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT NOT NULL UNIQUE, "
        "display_name TEXT NOT NULL, "
        "password_hash TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT 'dj', "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "last_login_at TEXT, "
        "avatar_url TEXT"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS user_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL REFERENCES users(id), "
        "refresh_token TEXT NOT NULL UNIQUE, "
        "device_info TEXT, "
        "ip_address TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "expires_at TEXT NOT NULL, "
        "revoked INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS api_keys ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL REFERENCES users(id), "
        "key_hash TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, "
        "permissions TEXT NOT NULL DEFAULT '[]', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "expires_at TEXT, "
        "is_active INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS station_outputs ("
        "station_id INTEGER PRIMARY KEY, "
        "local_output_enabled INTEGER DEFAULT 0, "
        "output_device_id TEXT DEFAULT '', "
        "icecast_enabled INTEGER NOT NULL DEFAULT 1, "
        "icecast_host TEXT NOT NULL DEFAULT '127.0.0.1', "
        "icecast_port INTEGER NOT NULL DEFAULT 8000, "
        "icecast_mount TEXT NOT NULL DEFAULT '/stream', "
        "icecast_user TEXT NOT NULL DEFAULT 'source', "
        "icecast_password TEXT NOT NULL DEFAULT '', "
        "output_gain_db REAL NOT NULL DEFAULT 0, "
        "stream_codec_profile TEXT NOT NULL DEFAULT 'he_aac_192', "
        "stream_bitrate_kbps INTEGER NOT NULL DEFAULT 192, "
        "source_protocol TEXT NOT NULL DEFAULT 'icecast'"
        ")"
    )
    _migrate_station_outputs(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS tracks ("
        "id INTEGER PRIMARY KEY, "
        "station_id INTEGER NOT NULL DEFAULT 1, "
        "title TEXT DEFAULT '', "
        "artist TEXT DEFAULT '', "
        "album TEXT NOT NULL DEFAULT '', "
        "genre TEXT NOT NULL DEFAULT '', "
        "language TEXT NOT NULL DEFAULT '', "
        "duration REAL NOT NULL DEFAULT 0, "
        "bpm REAL NOT NULL DEFAULT 0, "
        "track_type TEXT NOT NULL DEFAULT 'music', "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "exclude_from_autoplay INTEGER NOT NULL DEFAULT 0, "
        "play_count INTEGER NOT NULL DEFAULT 0, "
        "last_played_at TEXT, "
        "musicbrainz_recordingid TEXT DEFAULT '', "
        "managed_file_size INTEGER NOT NULL DEFAULT -1, "
        "managed_file_mtime_ns INTEGER NOT NULL DEFAULT -1, "
        "cover_art_url TEXT NOT NULL DEFAULT '', "
        "file_path TEXT DEFAULT ''"
        ")"
    )
    _migrate_tracks(cur)
    # Rights and repertoire metadata is kept separately so importing or
    # normalising a media file never destroys the operator's licensing record.
    cur.execute(
        "CREATE TABLE IF NOT EXISTS track_broadcast_metadata ("
        "track_id INTEGER PRIMARY KEY, version TEXT NOT NULL DEFAULT '', "
        "composer TEXT NOT NULL DEFAULT '', lyricist TEXT NOT NULL DEFAULT '', "
        "phonogram_producer TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '', "
        "isrc TEXT NOT NULL DEFAULT '', source_reference TEXT NOT NULL DEFAULT '', "
        "rights_reference TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT '', "
        "notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS music_usage_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER NOT NULL, "
        "queue_item_id INTEGER, track_id INTEGER, broadcast_at TEXT NOT NULL, "
        "work_title TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', "
        "performer TEXT NOT NULL DEFAULT '', composer TEXT NOT NULL DEFAULT '', "
        "lyricist TEXT NOT NULL DEFAULT '', phonogram_producer TEXT NOT NULL DEFAULT '', "
        "label TEXT NOT NULL DEFAULT '', isrc TEXT NOT NULL DEFAULT '', "
        "scheduled_duration_seconds REAL NOT NULL DEFAULT 0, "
        "played_duration_seconds REAL NOT NULL DEFAULT 0, publication_count INTEGER NOT NULL DEFAULT 1, "
        "source_path TEXT NOT NULL DEFAULT '', source_reference TEXT NOT NULL DEFAULT '', "
        "rights_reference TEXT NOT NULL DEFAULT '', program_name TEXT NOT NULL DEFAULT '', "
        "presenter TEXT NOT NULL DEFAULT '', delivered_variants_json TEXT NOT NULL DEFAULT '[]', "
        "log_id TEXT NOT NULL UNIQUE, "
        "metadata_snapshot_json TEXT NOT NULL DEFAULT '{}', previous_hash TEXT NOT NULL DEFAULT '', "
        "entry_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    music_usage_columns = {
        str(row[1])
        for row in cur.execute("PRAGMA table_info(music_usage_log)").fetchall()
    }
    if "delivered_variants_json" not in music_usage_columns:
        cur.execute(
            "ALTER TABLE music_usage_log ADD COLUMN delivered_variants_json "
            "TEXT NOT NULL DEFAULT '[]'"
        )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS music_usage_month_closures ("
        "period_key TEXT PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL, "
        "record_count INTEGER NOT NULL DEFAULT 0, first_entry_hash TEXT NOT NULL DEFAULT '', "
        "last_entry_hash TEXT NOT NULL DEFAULT '', export_path TEXT NOT NULL DEFAULT '', "
        "checksum TEXT NOT NULL, closed_by TEXT NOT NULL DEFAULT '', closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_music_usage_station_time "
        "ON music_usage_log(station_id, broadcast_at DESC, id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_music_usage_track_time "
        "ON music_usage_log(track_id, broadcast_at DESC, id DESC)"
    )
    cur.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_music_usage_log_no_update "
        "BEFORE UPDATE ON music_usage_log BEGIN SELECT RAISE(ABORT, 'music usage log is append-only'); END"
    )
    cur.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_music_usage_log_no_delete "
        "BEFORE DELETE ON music_usage_log BEGIN SELECT RAISE(ABORT, 'music usage log is append-only'); END"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS queue_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', enqueued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT, dedupe_key TEXT)"
    )
    _migrate_queue_items(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS station_worker_lease (station_id INTEGER PRIMARY KEY, worker_id TEXT NOT NULL, lease_expires_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS playout_state (station_id INTEGER PRIMARY KEY, current_source TEXT NOT NULL DEFAULT 'none', current_item_id INTEGER, started_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS playout_transitions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "station_id INTEGER NOT NULL, "
        "from_source TEXT NOT NULL DEFAULT 'none', "
        "from_item_id INTEGER, "
        "to_source TEXT NOT NULL DEFAULT 'none', "
        "to_item_id INTEGER, "
        "reason TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "FOREIGN KEY(station_id) REFERENCES stations(id) ON DELETE CASCADE"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_playout_transitions_station_time "
        "ON playout_transitions(station_id, id DESC)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schedule_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, play_at TEXT NOT NULL, window_end TEXT, event_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending')"
    )
    _migrate_schedule_items(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ad_break_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', priority INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT, dedupe_key TEXT)"
    )
    _migrate_ad_break_items(cur)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_station_status_pos ON queue_items(station_id, status, position)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_active_dedupe "
        "ON queue_items(station_id, dedupe_key) "
        "WHERE status IN ('pending', 'playing') "
        "AND dedupe_key IS NOT NULL AND TRIM(dedupe_key) <> ''"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_station_status_play_at ON schedule_items(station_id, status, play_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ads_station_status_due_at ON ad_break_items(station_id, status, due_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_mbid ON tracks(musicbrainz_recordingid)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS command_outbox (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, command_type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS playlists ("
        "id INTEGER PRIMARY KEY, "
        "station_id INTEGER NOT NULL, "
        "name TEXT NOT NULL, "
        "description TEXT NOT NULL DEFAULT '', "
        "playlist_type TEXT NOT NULL DEFAULT 'manual', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    _migrate_playlists(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS playlist_items (id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS station_settings (station_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (station_id, key))"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS daypart_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "station_id INTEGER NOT NULL, "
        "day_of_week INTEGER NOT NULL DEFAULT 0, "
        "position INTEGER NOT NULL DEFAULT 0, "
        "name TEXT NOT NULL, "
        "start_minute INTEGER NOT NULL, "
        "end_minute INTEGER NOT NULL, "
        "min_bpm REAL NOT NULL, "
        "max_bpm REAL NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(station_id, day_of_week, position), "
        "FOREIGN KEY(station_id) REFERENCES stations(id) ON DELETE CASCADE, "
        "CHECK(day_of_week >= 0 AND day_of_week <= 6), "
        "CHECK(start_minute >= 0 AND start_minute < 1440), "
        "CHECK(end_minute >= 0 AND end_minute < 1440), "
        "CHECK(start_minute <> end_minute), "
        "CHECK(min_bpm >= 30 AND max_bpm <= 240 AND min_bpm <= max_bpm)"
        ")"
    )
    _migrate_daypart_rules(cur)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_daypart_rules_station_position "
        "ON daypart_rules(station_id, day_of_week, enabled, position)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS operation_logs (id INTEGER PRIMARY KEY, station_id INTEGER, level TEXT NOT NULL DEFAULT 'info', event_type TEXT NOT NULL DEFAULT 'operation', message TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS audit_chain ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER, category TEXT NOT NULL, "
        "action TEXT NOT NULL, actor_id INTEGER, payload_json TEXT NOT NULL DEFAULT '{}', "
        "previous_hash TEXT NOT NULL DEFAULT '', entry_hash TEXT NOT NULL UNIQUE, "
        "witness_anchor TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS witness_audit_anchors ("
        "entry_hash TEXT PRIMARY KEY, node_id TEXT NOT NULL, signature TEXT NOT NULL, "
        "anchored_at TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ha_state ("
        "id INTEGER PRIMARY KEY CHECK(id=1), current_term INTEGER NOT NULL DEFAULT 0, "
        "voted_for TEXT NOT NULL DEFAULT '', leader_id TEXT NOT NULL DEFAULT '', "
        "leader_lease_expires_at REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "INSERT INTO ha_state(id) VALUES (1) ON CONFLICT(id) DO NOTHING"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS replication_journal ("
        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, "
        "entity_id TEXT NOT NULL DEFAULT '', operation TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "checksum TEXT NOT NULL UNIQUE, replicated_at TEXT, applied_at TEXT, committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    # A short-lived pre-release v13 build created this table without the
    # standby-application marker.  Keep bootstrap idempotent for those nodes.
    cur.execute("PRAGMA table_info(replication_journal)")
    replication_columns = {str(row[1]) for row in cur.fetchall()}
    if "applied_at" not in replication_columns:
        cur.execute("ALTER TABLE replication_journal ADD COLUMN applied_at TEXT")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS media_manifests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, root_path TEXT NOT NULL, "
        "manifest_hash TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0, "
        "payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ha_playout_checkpoints ("
        "station_id INTEGER PRIMARY KEY, node_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "checksum TEXT NOT NULL, checkpointed_at REAL NOT NULL, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS recovery_points ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, tier TEXT NOT NULL, file_path TEXT NOT NULL UNIQUE, "
        "sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, integrity_status TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, verified_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS stream_config_drafts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE, "
        "revision INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL, config_hash TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'draft', validation_json TEXT NOT NULL DEFAULT '{}', "
        "created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS stream_config_operations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL REFERENCES stream_config_drafts(id), "
        "station_id INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
        "previous_config_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}', "
        "created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS guest_invites ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, studio_id INTEGER NOT NULL REFERENCES studios(id) ON DELETE CASCADE, "
        "station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE, token_hash TEXT NOT NULL UNIQUE, "
        "created_by INTEGER NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT, revoked_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS guest_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, invite_id INTEGER NOT NULL REFERENCES guest_invites(id) ON DELETE CASCADE, "
        "studio_id INTEGER NOT NULL, station_id INTEGER NOT NULL, display_name TEXT NOT NULL, "
        "session_token_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'lobby', "
        "is_connected INTEGER NOT NULL DEFAULT 0, is_muted INTEGER NOT NULL DEFAULT 0, "
        "is_on_air INTEGER NOT NULL DEFAULT 0, gain_db REAL NOT NULL DEFAULT 0, "
        "connection_quality TEXT NOT NULL DEFAULT 'unknown', admitted_at TEXT, left_at TEXT, "
        "last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS guest_recordings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, studio_id INTEGER NOT NULL, station_id INTEGER NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending_consent', manifest_json TEXT NOT NULL DEFAULT '{}', "
        "file_path TEXT NOT NULL DEFAULT '', started_by INTEGER NOT NULL, started_at TEXT, stopped_at TEXT, "
        "expires_at TEXT, interruption_reason TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS guest_recording_consents ("
        "recording_id INTEGER NOT NULL REFERENCES guest_recordings(id) ON DELETE CASCADE, "
        "session_id INTEGER NOT NULL REFERENCES guest_sessions(id) ON DELETE CASCADE, "
        "decision TEXT NOT NULL DEFAULT 'pending', decided_at TEXT, "
        "PRIMARY KEY(recording_id, session_id))"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, event_type TEXT NOT NULL DEFAULT 'generic', payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS program_queue_items (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ad_break_sets ("
        "id INTEGER PRIMARY KEY, "
        "station_id INTEGER NOT NULL, "
        "name TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "payload_json TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    _migrate_ad_break_sets(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ad_campaigns (id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS broadcast_campaigns ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
        "starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'YouTube playlist', "
        "voting_enabled INTEGER NOT NULL DEFAULT 1, ai_enabled INTEGER NOT NULL DEFAULT 1, "
        "restore_policy TEXT NOT NULL DEFAULT 'keep_campaign_library', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS broadcast_campaign_stations ("
        "campaign_id INTEGER NOT NULL REFERENCES broadcast_campaigns(id) ON DELETE CASCADE, "
        "station_id INTEGER NOT NULL REFERENCES stations(id), genre TEXT NOT NULL, "
        "managed_folder TEXT NOT NULL DEFAULT '', previous_folder TEXT NOT NULL DEFAULT '', "
        "previous_mode TEXT NOT NULL DEFAULT 'replace', last_selected_at TEXT, "
        "PRIMARY KEY (campaign_id, station_id), UNIQUE (campaign_id, genre))"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS genre_voting_rounds ("
        "id TEXT PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES broadcast_campaigns(id) ON DELETE CASCADE, "
        "status TEXT NOT NULL DEFAULT 'open', opens_at TEXT NOT NULL, closes_at TEXT NOT NULL, "
        "winning_genre TEXT NOT NULL DEFAULT '', queued_track_id INTEGER REFERENCES tracks(id), "
        "resolved_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS genre_votes ("
        "id INTEGER PRIMARY KEY, round_id TEXT NOT NULL REFERENCES genre_voting_rounds(id) ON DELETE CASCADE, "
        "genre TEXT NOT NULL, voter_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE (round_id, voter_hash))"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_campaign_window "
        "ON broadcast_campaigns(enabled, starts_at, ends_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_genre_votes_round_genre "
        "ON genre_votes(round_id, genre)"
    )
    cur.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_genre_votes_no_update "
        "BEFORE UPDATE ON genre_votes BEGIN SELECT RAISE(ABORT, 'genre votes are append-only'); END"
    )
    cur.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_genre_votes_no_delete "
        "BEFORE DELETE ON genre_votes BEGIN SELECT RAISE(ABORT, 'genre votes are append-only'); END"
    )
    _migrate_studios(cur)
    _migrate_studio_sessions(cur)
    _migrate_studio_chat_messages(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS metadata_rules ("
        "id INTEGER PRIMARY KEY, "
        "station_id INTEGER, "
        "scope TEXT NOT NULL DEFAULT 'station', "
        "name TEXT NOT NULL DEFAULT '', "
        "target_field TEXT NOT NULL DEFAULT 'title', "
        "match_type TEXT NOT NULL DEFAULT 'contains', "
        "pattern TEXT NOT NULL DEFAULT '', "
        "replacement TEXT NOT NULL DEFAULT '', "
        "is_case_sensitive INTEGER NOT NULL DEFAULT 0, "
        "priority INTEGER NOT NULL DEFAULT 100, "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_playlists_station ON playlists(station_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist_pos ON playlist_items(playlist_id, position)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_station_settings_station ON station_settings(station_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_logs_station_created ON operation_logs(station_id, created_at DESC)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_chain_station_id ON audit_chain(station_id, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_replication_pending ON replication_journal(replicated_at, sequence)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stream_drafts_station ON stream_config_drafts(station_id, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_invites_studio ON guest_invites(studio_id, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_studio_status ON guest_sessions(studio_id, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_guest_recordings_station ON guest_recordings(station_id, id DESC)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_station_created ON events(station_id, created_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_program_queue_station_pos ON program_queue_items(station_id, position)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_break_sets_station ON ad_break_sets(station_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_campaigns_station ON ad_campaigns(station_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_station_type_active ON tracks(station_id, track_type, is_active)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_rules_scope_station_active_priority ON metadata_rules(scope, station_id, is_active, priority)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS soundboard_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE, "
        "name TEXT NOT NULL, "
        "file_path TEXT NOT NULL, "
        "uploaded INTEGER DEFAULT 0, "
        "color TEXT DEFAULT '#4a90d9', "
        "hotkey TEXT, "
        "category TEXT DEFAULT 'general', "
        "duration_s REAL, "
        "gain_db REAL DEFAULT 0.0, "
        "sort_order INTEGER DEFAULT 0"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_soundboard_items_station_sort "
        "ON soundboard_items(station_id, sort_order)"
    )
    # --- shows ---
    cur.execute(
        "CREATE TABLE IF NOT EXISTS shows ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "station_id INTEGER NOT NULL REFERENCES stations(id), "
        "name TEXT NOT NULL, "
        "description TEXT, "
        "color TEXT DEFAULT '#4a90d9', "
        "intro_path TEXT, "
        "outro_path TEXT, "
        "break_outro_path TEXT, "
        "break_intro_path TEXT, "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shows_station_active "
        "ON shows(station_id, is_active)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS show_assignments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE, "
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "role TEXT NOT NULL DEFAULT 'dj', "
        "permission_keys_json TEXT NOT NULL DEFAULT '', "
        "UNIQUE(show_id, user_id)"
        ")"
    )
    _migrate_show_assignments(cur)
    _migrate_show_assignment_permissions(cur)
    cur.execute(
        "CREATE TABLE IF NOT EXISTS show_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "show_id INTEGER NOT NULL REFERENCES shows(id), "
        "station_id INTEGER NOT NULL REFERENCES stations(id), "
        "user_id INTEGER NOT NULL REFERENCES users(id), "
        "status TEXT NOT NULL DEFAULT 'preparing', "
        "started_at TEXT, "
        "ended_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_show_sessions_active_station "
        "ON show_sessions(station_id) WHERE status NOT IN ('ended')"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS role_templates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "description TEXT NOT NULL DEFAULT '', "
        "is_system INTEGER NOT NULL DEFAULT 0, "
        "is_active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS role_template_permissions ("
        "role_template_id INTEGER NOT NULL REFERENCES role_templates(id) ON DELETE CASCADE, "
        "permission_key TEXT NOT NULL, "
        "PRIMARY KEY(role_template_id, permission_key)"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS user_role_assignments ("
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "role_template_id INTEGER NOT NULL REFERENCES role_templates(id) ON DELETE CASCADE, "
        "PRIMARY KEY(user_id, role_template_id)"
        ")"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS show_assignment_permissions ("
        "show_id INTEGER NOT NULL, "
        "user_id INTEGER NOT NULL, "
        "permission_key TEXT NOT NULL, "
        "PRIMARY KEY(show_id, user_id, permission_key), "
        "FOREIGN KEY(show_id, user_id) REFERENCES show_assignments(show_id, user_id) ON DELETE CASCADE"
        ")"
    )


def init_db(*, product_mode: str | None = None):
    # Endpoints call init_db() frequently; only the first successful bootstrap
    # should perform schema/data writes. Later calls must stay read-only.
    with _INIT_LOCK:
        db_path = get_db_path()
        existing_database = db_path.is_file() and db_path.stat().st_size > 0
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version")
            current_version = int(cur.fetchone()[0] or 0)
            if current_version > _SCHEMA_VERSION:
                return
            schema_bootstrap_applied = _schema_bootstrap_applied(cur)
            if (
                current_version == _SCHEMA_VERSION
                and schema_bootstrap_applied
                and not _legacy_rbac_needs_sync(cur)
                and not _post_version_repairs_needed(cur)
            ):
                return

            if existing_database:
                _backup_database_before_schema_migration(db_path)
            _bootstrap_schema(cur)
            _ensure_default_admin(cur)
            _ensure_default_station(cur)
            _migrate_station_credentials(cur)
            _repair_rbac_foreign_key_integrity(cur)
            if _legacy_rbac_needs_sync(cur):
                _sync_legacy_rbac(cur)
            _backfill_default_studios(cur)
            conn.commit()

            from app.repositories.settings_repo import SettingsRepository

            SettingsRepository(conn).ensure_system_defaults()
            _mark_schema_bootstrap_applied(cur)
            cur.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
