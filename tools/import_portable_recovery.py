"""Restore a RadioTEDU portable recovery bundle without logging secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.security.credential_vault import get_credential_vault
from tools.portable_recovery_crypto import decrypt_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_manifest(bundle: Path) -> None:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest.get("files", []):
        path = (bundle / str(item["path"])).resolve()
        try:
            path.relative_to(bundle)
        except ValueError as exc:
            raise RuntimeError("Recovery manifest path escaped the bundle") from exc
        if not path.is_file() or _sha256(path) != str(item["sha256"]):
            raise RuntimeError(f"Recovery file verification failed: {item['path']}")


def _copy_database(source: Path, target: Path) -> Path | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.parent / "Recovery" / f"cleanroom.pre-portable-import-{stamp}.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    temporary = target.with_suffix(".portable-import.tmp.db")
    temporary.unlink(missing_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("Restored database failed integrity_check")
            dst.commit()
    os.replace(temporary, target)
    return backup


def _mapped_path(value: str, drive: str, media_root: Path) -> str | None:
    normalized_drive = drive.rstrip("\\/").upper()
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if not match or match.group(1).upper() + ":" != normalized_drive:
        return None
    remainder = match.group(2).replace("\\", "/")
    return str((media_root / Path(remainder)).resolve(strict=False))


def _rewrite_media_paths(database: Path, drive: str, media_root: Path) -> int:
    changed = 0
    with sqlite3.connect(database) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table_row in tables:
            table = str(table_row[0])
            columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            for column in columns:
                name = str(column[1])
                declared = str(column[2] or "").upper()
                if "TEXT" not in declared or not re.search(
                    r"path|file|folder|root|directory|uri|value", name, re.I
                ):
                    continue
                try:
                    rows = conn.execute(
                        f'SELECT rowid, "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'
                    ).fetchall()
                except sqlite3.DatabaseError:
                    continue
                for rowid, raw in rows:
                    replacement = _mapped_path(str(raw or ""), drive, media_root)
                    if replacement is None or replacement == raw:
                        continue
                    try:
                        conn.execute(
                            f'UPDATE "{table}" SET "{name}"=? WHERE rowid=?',
                            (replacement, rowid),
                        )
                    except sqlite3.DatabaseError:
                        continue
                    changed += 1
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("Database failed integrity_check after path mapping")
        conn.commit()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--user-config-root", type=Path, required=True)
    parser.add_argument("--media-root", type=Path)
    parser.add_argument("--source-drive", default="H:")
    parser.add_argument("--password-env", default="RADIOTEDU_BACKUP_PASSWORD")
    args = parser.parse_args()

    password = os.getenv(args.password_env, "")
    if not password:
        raise RuntimeError(f"Recovery password environment variable is missing: {args.password_env}")
    bundle = args.bundle_root.resolve()
    _verify_manifest(bundle)
    secrets_payload = decrypt_json(
        (bundle / "private" / "portable-secrets.bin").read_bytes(), password
    )
    credentials = secrets_payload.get("credential_values")
    if not isinstance(credentials, dict):
        raise RuntimeError("Portable credential payload is invalid")

    data_root = args.data_root.expanduser().resolve()
    user_root = args.user_config_root.expanduser().resolve()
    target_db = data_root / "cleanroom.db"
    backup = _copy_database(bundle / "private" / "cleanroom.db", target_db)
    rewritten = 0
    if args.media_root is not None:
        rewritten = _rewrite_media_paths(
            target_db,
            args.source_drive,
            args.media_root.expanduser().resolve(strict=False),
        )

    os.environ["CLEANROOM_DATA_ROOT"] = str(data_root)
    os.environ["CLEANROOM_DB_PATH"] = str(target_db)
    os.environ["CLEANROOM_USER_CONFIG_ROOT"] = str(user_root)
    os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(
        user_root / "secrets" / "station-credentials.json"
    )
    os.environ.pop("CLEANROOM_CREDENTIAL_DPAPI_SCOPE", None)
    imported = get_credential_vault().import_secrets(
        {str(key): str(value) for key, value in credentials.items()}
    )
    print(
        json.dumps(
            {
                "ok": True,
                "database": str(target_db),
                "database_backup_created": bool(backup),
                "credentials_imported": imported,
                "media_paths_rewritten": rewritten,
                "database_integrity_check": "ok",
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
