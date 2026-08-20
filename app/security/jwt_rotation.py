from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_ROTATION_MARKER_KEY = "__jwt_rotation_v1_0_2__"


def _assert_database_integrity(database_path: Path) -> None:
    with closing(sqlite3.connect(str(database_path))) as conn:
        quick_check = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"JWT rotation blocked by SQLite integrity: {quick_check}")
        violation = conn.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise RuntimeError(
                "JWT rotation blocked by foreign-key violation: "
                f"{violation[0]}->{violation[2]}"
            )


def _verified_backup(database_path: Path, recovery_root: Path) -> Path:
    recovery_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = recovery_root / (
        f"{database_path.stem}.before-jwt-rotation-{timestamp}-{secrets.token_hex(4)}"
        f"{database_path.suffix or '.sqlite3'}"
    )
    temporary_path = backup_path.with_name(f".{backup_path.name}.tmp")
    try:
        with closing(sqlite3.connect(str(database_path))) as source:
            with closing(sqlite3.connect(str(temporary_path))) as destination:
                source.backup(destination)
        _assert_database_integrity(temporary_path)
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary_path, backup_path)
        return backup_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _stage_secret(secret_path: Path) -> tuple[Path, str]:
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = secret_path.with_name(
        f".{secret_path.name}.{secrets.token_hex(6)}.rotation.tmp"
    )
    value = secrets.token_urlsafe(64)
    descriptor = os.open(staged_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise
    return staged_path, value


def rotate_jwt_secret(
    *,
    database_path: Path,
    secret_path: Path,
    recovery_root: Path,
) -> dict[str, object]:
    database_path = Path(database_path).expanduser().resolve()
    secret_path = Path(secret_path).expanduser().resolve()
    recovery_root = Path(recovery_root).expanduser().resolve()
    if not database_path.is_file() or database_path.stat().st_size <= 0:
        raise RuntimeError("JWT rotation requires an existing non-empty database")
    if secret_path == database_path:
        raise RuntimeError("JWT secret path must not be the database path")

    _assert_database_integrity(database_path)
    backup_path = _verified_backup(database_path, recovery_root)
    staged_path, _secret_value = _stage_secret(secret_path)
    rotated_at = datetime.now(timezone.utc).isoformat()
    replaced = False
    try:
        with closing(sqlite3.connect(str(database_path), timeout=30.0)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN EXCLUSIVE")
            session_update = conn.execute(
                "UPDATE user_sessions SET revoked=1 WHERE revoked=0"
            )
            revoked_sessions = max(0, int(session_update.rowcount))
            marker = json.dumps(
                {
                    "reason": "acl-exposure-remediation",
                    "revoked_sessions": revoked_sessions,
                    "rotated_at": rotated_at,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            conn.execute(
                "INSERT INTO system_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (_ROTATION_MARKER_KEY, marker),
            )
            os.replace(staged_path, secret_path)
            replaced = True
            conn.commit()
    finally:
        staged_path.unlink(missing_ok=True)

    if not replaced:
        raise RuntimeError("JWT rotation did not replace the signing key")
    return {
        "backup_path": str(backup_path),
        "database_path": str(database_path),
        "revoked_sessions": revoked_sessions,
        "rotated_at": rotated_at,
        "status": "rotated",
    }
