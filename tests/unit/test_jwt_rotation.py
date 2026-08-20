import json
import sqlite3
import sys

import pytest

from app.db import init_db
from app.security.jwt_rotation import rotate_jwt_secret
from run_cleanroom import main as run_main


def _initialized_database(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    database_path = data_root / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(database_path))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    init_db()
    return database_path


def test_jwt_rotation_is_backup_first_atomic_and_secret_safe(tmp_path, monkeypatch):
    database_path = _initialized_database(tmp_path, monkeypatch)
    secret_path = tmp_path / "data" / "secrets" / "jwt-signing.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    old_secret = "old-signing-key-canary-value"
    secret_path.write_text(old_secret, encoding="utf-8")

    with sqlite3.connect(str(database_path)) as conn:
        user_id = int(conn.execute("SELECT MIN(id) FROM users").fetchone()[0])
        conn.execute(
            "INSERT INTO user_sessions "
            "(user_id, refresh_token, expires_at, revoked) VALUES (?, ?, ?, 0)",
            (user_id, "hashed-refresh-token", "2099-01-01T00:00:00+00:00"),
        )
        conn.commit()

    result = rotate_jwt_secret(
        database_path=database_path,
        secret_path=secret_path,
        recovery_root=tmp_path / "data" / "Recovery" / "jwt-rotation",
    )

    new_secret = secret_path.read_text(encoding="utf-8")
    assert new_secret != old_secret
    assert len(new_secret) >= 64
    assert old_secret not in json.dumps(result)
    assert new_secret not in json.dumps(result)
    assert result["status"] == "rotated"
    assert result["revoked_sessions"] == 1

    backup_path = result["backup_path"]
    with sqlite3.connect(str(backup_path)) as backup:
        assert backup.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup.execute("SELECT revoked FROM user_sessions").fetchone()[0] == 0

    with sqlite3.connect(str(database_path)) as conn:
        assert conn.execute("SELECT revoked FROM user_sessions").fetchone()[0] == 1
        marker = conn.execute(
            "SELECT value FROM system_settings "
            "WHERE key='__jwt_rotation_v1_0_2__'"
        ).fetchone()[0]
        marker_payload = json.loads(marker)
        assert marker_payload["reason"] == "acl-exposure-remediation"
        assert marker_payload["revoked_sessions"] == 1


def test_jwt_rotation_refuses_foreign_key_damage_without_touching_secret(
    tmp_path, monkeypatch
):
    database_path = _initialized_database(tmp_path, monkeypatch)
    secret_path = tmp_path / "data" / "secrets" / "jwt-signing.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    old_secret = "must-remain-unchanged"
    secret_path.write_text(old_secret, encoding="utf-8")

    with sqlite3.connect(str(database_path)) as conn:
        missing_role_id = (
            int(conn.execute("SELECT MAX(id) FROM role_templates").fetchone()[0]) + 1000
        )
        conn.execute(
            "INSERT INTO role_template_permissions (role_template_id, permission_key) "
            "VALUES (?, ?)",
            (missing_role_id, "stations.view"),
        )
        conn.commit()

    recovery_root = tmp_path / "data" / "Recovery" / "jwt-rotation"
    with pytest.raises(
        RuntimeError,
        match="foreign-key violation: role_template_permissions->role_templates",
    ):
        rotate_jwt_secret(
            database_path=database_path,
            secret_path=secret_path,
            recovery_root=recovery_root,
        )

    assert secret_path.read_text(encoding="utf-8") == old_secret
    assert not recovery_root.exists()


def test_offline_rotation_command_reports_evidence_without_secret_values(
    tmp_path, monkeypatch, capsys
):
    _initialized_database(tmp_path, monkeypatch)
    secret_path = tmp_path / "data" / "secrets" / "jwt-signing.key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    old_secret = "offline-command-old-secret-canary"
    secret_path.write_text(old_secret, encoding="utf-8")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.setenv("CLEANROOM_JWT_SECRET_FILE", str(secret_path))
    monkeypatch.setattr(sys, "argv", ["run_cleanroom.py", "rotate-jwt-secret"])

    run_main()

    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    new_secret = secret_path.read_text(encoding="utf-8")
    assert payload["status"] == "rotated"
    assert old_secret not in output
    assert new_secret not in output
