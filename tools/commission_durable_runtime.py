from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DB = REPO_ROOT / "run" / "new-program" / "data" / "cleanroom.db"
DEFAULT_SOURCE_JWT = REPO_ROOT / "run" / "new-program" / "user" / "secrets" / "jwt-signing.key"
DEFAULT_DATA_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir"
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {destination}: {result}")


def _install_sqlite(source: Path, destination: Path) -> None:
    """Install via SQLite's online-backup API so open read handles stay valid."""
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {destination}: {result}")


def _validate_outputs(database: Path) -> list[dict[str, object]]:
    with sqlite3.connect(database) as conn:
        invalid = conn.execute(
            "SELECT station_id, icecast_mount FROM station_outputs "
            "WHERE icecast_enabled = 1 AND (TRIM(icecast_mount) = '' OR icecast_mount NOT LIKE '/%')"
        ).fetchall()
        if invalid:
            raise RuntimeError(f"Invalid enabled Icecast mounts: {invalid}")
        duplicates = conn.execute(
            "SELECT icecast_host, icecast_port, icecast_mount, COUNT(*) "
            "FROM station_outputs WHERE icecast_enabled = 1 "
            "GROUP BY icecast_host, icecast_port, icecast_mount HAVING COUNT(*) > 1"
        ).fetchall()
        if duplicates:
            raise RuntimeError(f"Duplicate enabled Icecast outputs remain: {duplicates}")
        conn.commit()
        rows = conn.execute(
            "SELECT station_id, icecast_host, icecast_port, icecast_mount, "
            "icecast_password FROM station_outputs WHERE icecast_enabled = 1 "
            "ORDER BY station_id"
        ).fetchall()
        result = conn.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed after mount repair: {result}")
    return [
        {
            "station_id": int(row[0]),
            "host": str(row[1]),
            "port": int(row[2]),
            "mount": str(row[3]),
            "credential_reference": str(row[4]),
        }
        for row in rows
    ]


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.commissioning.tmp")
    temporary.unlink(missing_ok=True)
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Commission the current RadioTEDU state into durable machine storage."
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--source-jwt", type=Path, default=DEFAULT_SOURCE_JWT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    source_db = args.source_db.resolve()
    source_jwt = args.source_jwt.resolve()
    data_root = args.data_root.resolve()
    if not source_db.is_file() or not source_jwt.is_file():
        raise FileNotFoundError("The active database or JWT signing key is missing")

    secrets_dir = data_root / "secrets"
    recovery_dir = data_root / "Recovery" / "commissioning"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination_db = data_root / "cleanroom.db"
    destination_jwt = secrets_dir / "jwt-signing.key"

    if destination_db.exists():
        _sqlite_backup(destination_db, recovery_dir / f"cleanroom-before-{timestamp}.db")
    if destination_jwt.exists():
        _atomic_copy(destination_jwt, recovery_dir / f"jwt-signing-before-{timestamp}.key")

    db_temp = data_root / ".cleanroom.commissioning.tmp.db"
    _sqlite_backup(source_db, db_temp)
    outputs = _validate_outputs(db_temp)
    _install_sqlite(db_temp, destination_db)
    try:
        db_temp.unlink(missing_ok=True)
    except PermissionError:
        # An AV/indexer can briefly retain the staging file on Windows.  It is
        # harmless and will be replaced on the next commissioning run.
        pass
    _atomic_copy(source_jwt, destination_jwt)

    vault_path = secrets_dir / "station-credentials.json"
    os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(vault_path)
    os.environ["CLEANROOM_CREDENTIAL_DPAPI_SCOPE"] = "machine"
    sys.path.insert(0, str(REPO_ROOT))
    from app.security.credential_vault import (
        credential_reference,
        get_credential_vault,
        is_credential_reference,
    )

    vault = get_credential_vault()
    with sqlite3.connect(destination_db) as conn:
        for output in outputs:
            station_id = int(output["station_id"])
            stored_value = str(output["credential_reference"])
            reference = (
                stored_value
                if is_credential_reference(stored_value)
                else credential_reference(station_id)
            )
            if not is_credential_reference(stored_value):
                if not stored_value:
                    raise RuntimeError(f"Station {station_id} has no Icecast credential")
                vault.set_secret(reference, stored_value)
                conn.execute(
                    "UPDATE station_outputs SET icecast_password = ? WHERE station_id = ?",
                    (reference, station_id),
                )
                conn.execute(
                    "INSERT INTO station_settings (station_id, key, value, updated_at) "
                    "VALUES (?, 'icecast_password', ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(station_id, key) DO UPDATE SET "
                    "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (station_id, reference),
                )
            output["credential_reference"] = reference
        conn.commit()
    unresolved = [
        int(output["station_id"])
        for output in outputs
        if not vault.get_secret(str(output["credential_reference"]))
    ]
    if unresolved:
        raise RuntimeError(f"Credential vault cannot resolve station IDs: {unresolved}")

    manifest = {
        "commissioned_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(destination_db),
        "database_sha256": _sha256(destination_db),
        "jwt_signing_key": str(destination_jwt),
        "jwt_signing_key_sha256": _sha256(destination_jwt),
        "credential_vault": str(vault_path),
        "credential_station_ids_verified": [int(item["station_id"]) for item in outputs],
        "outputs": [
            {key: value for key, value in item.items() if key != "credential_reference"}
            for item in outputs
        ],
    }
    manifest_path = recovery_dir / f"commissioned-{timestamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "manifest": str(manifest_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
