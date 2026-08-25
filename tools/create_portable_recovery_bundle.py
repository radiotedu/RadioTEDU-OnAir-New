"""Create a private, cross-platform RadioTEDU recovery staging directory.

The resulting directory is meant to be placed inside an encrypted archive.
Secret values are never printed and are themselves AES-GCM encrypted before
the outer archive is created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.security.credential_vault import CredentialVault, is_credential_reference
from tools.portable_recovery_crypto import encrypt_json


WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):[\\/]")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("Portable database snapshot failed integrity_check")


def _git_value(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _copy_tracked_source(repository: Path, target: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-z"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    names = [item for item in result.stdout.decode("utf-8").split("\x00") if item]
    copied = 0
    for name in names:
        source = (repository / name).resolve()
        try:
            source.relative_to(repository.resolve())
        except ValueError as exc:
            raise RuntimeError("Tracked source path escaped the repository") from exc
        if not source.is_file() or source.is_symlink():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    return copied


def _merge_credentials(stores: list[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for store in stores:
        if not store.is_file():
            continue
        values = CredentialVault(store).export_secrets()
        for reference, secret in values.items():
            existing = merged.get(reference)
            if existing is not None and existing != secret:
                raise RuntimeError(
                    f"Credential reference differs between vaults: {reference}"
                )
            merged[reference] = secret
    return merged


def _resolved(value: object, credentials: dict[str, str]) -> str:
    stored = str(value or "")
    if is_credential_reference(stored):
        if stored not in credentials:
            raise RuntimeError(f"Portable credential is missing for {stored}")
        return credentials[stored]
    return stored


def _extra_outputs(raw: str, credentials: dict[str, str]) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    output = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        key = "icecast_password" if "icecast_password" in row else "password"
        if key in row:
            row[key] = _resolved(row.get(key), credentials)
        output.append(row)
    return output


def _collect_stream_configuration(
    database: Path, credentials: dict[str, str]
) -> tuple[list[dict], list[dict], list[str]]:
    secret_rows: list[dict] = []
    public_rows: list[dict] = []
    media_drives: set[str] = set()
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        extra_by_station: dict[int, str] = {}
        for row in conn.execute(
            "SELECT key, value FROM system_settings "
            "WHERE key LIKE 'station_%_extra_icecast_outputs'"
        ):
            match = re.match(r"station_(\d+)_extra_icecast_outputs", str(row["key"]))
            if match:
                extra_by_station[int(match.group(1))] = str(row["value"] or "")
        rows = conn.execute(
            "SELECT s.id, s.name, o.* FROM stations s "
            "LEFT JOIN station_outputs o ON o.station_id=s.id ORDER BY s.id"
        ).fetchall()
        for row in rows:
            station_id = int(row["id"])
            primary = None
            if row["station_id"] is not None:
                primary = {
                    "enabled": bool(row["icecast_enabled"]),
                    "host": str(row["icecast_host"] or ""),
                    "port": int(row["icecast_port"] or 0),
                    "mount": str(row["icecast_mount"] or ""),
                    "user": str(row["icecast_user"] or ""),
                    "password": _resolved(row["icecast_password"], credentials),
                    "source_protocol": str(row["source_protocol"] or "icecast"),
                    "codec_profile": str(row["stream_codec_profile"] or ""),
                    "bitrate_kbps": int(row["stream_bitrate_kbps"] or 0),
                    "local_output_enabled": bool(row["local_output_enabled"]),
                }
            extras = _extra_outputs(extra_by_station.get(station_id, ""), credentials)
            secret_rows.append(
                {
                    "station_id": station_id,
                    "station_name": str(row["name"] or ""),
                    "primary": primary,
                    "extra_outputs": extras,
                }
            )
            public_primary = None if primary is None else {
                key: value for key, value in primary.items() if key != "password"
            }
            if public_primary is not None:
                public_primary["password_in_encrypted_recovery"] = bool(primary["password"])
            public_extras = []
            for item in extras:
                safe = {
                    key: value
                    for key, value in item.items()
                    if key not in {"password", "icecast_password"}
                }
                safe["password_in_encrypted_recovery"] = bool(
                    item.get("password") or item.get("icecast_password")
                )
                public_extras.append(safe)
            public_rows.append(
                {
                    "station_id": station_id,
                    "station_name": str(row["name"] or ""),
                    "primary": public_primary,
                    "extra_outputs": public_extras,
                }
            )

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
                    r"path|file|folder|root|directory|uri", name, re.I
                ):
                    continue
                try:
                    values = conn.execute(
                        f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'
                    )
                    for value_row in values:
                        match = WINDOWS_DRIVE.match(str(value_row[0] or ""))
                        if match:
                            media_drives.add(match.group(1).upper() + ":")
                except sqlite3.DatabaseError:
                    continue
    return secret_rows, public_rows, sorted(media_drives)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--credential-store", type=Path, action="append", default=[])
    parser.add_argument("--windows-tools-root", type=Path)
    parser.add_argument("--password-env", default="RADIOTEDU_BACKUP_PASSWORD")
    args = parser.parse_args()

    password = os.getenv(args.password_env, "")
    if not password:
        raise RuntimeError(f"Recovery password environment variable is missing: {args.password_env}")
    repository = args.repository_root.resolve()
    live_root = args.live_root.resolve()
    destination = args.destination.resolve()
    if destination.exists():
        raise FileExistsError("Recovery staging destination already exists")
    source_db = (
        args.database.resolve()
        if args.database is not None
        else live_root / "data" / "cleanroom.db"
    )
    if not source_db.is_file():
        raise FileNotFoundError(f"Live RadioTEDU database is missing: {source_db}")

    destination.mkdir(parents=True, exist_ok=False)
    try:
        source_count = _copy_tracked_source(repository, destination / "app")
        snapshot_db = destination / "private" / "cleanroom.db"
        _sqlite_backup(source_db, snapshot_db)
        credentials = _merge_credentials([path.resolve() for path in args.credential_store])
        secret_streams, public_streams, media_drives = _collect_stream_configuration(
            snapshot_db, credentials
        )
        created_at = datetime.now(timezone.utc).isoformat()
        private_payload = {
            "schema_version": 1,
            "created_at": created_at,
            "credential_values": credentials,
            "stream_outputs": secret_streams,
            "source_media_drives": media_drives,
        }
        secret_path = destination / "private" / "portable-secrets.bin"
        secret_path.write_bytes(encrypt_json(private_payload, password))
        _write_json(
            destination / "stream-mounts.json",
            {
                "schema_version": 1,
                "created_at": created_at,
                "passwords": "encrypted in private/portable-secrets.bin",
                "stations": public_streams,
            },
        )

        tools_included = False
        if args.windows_tools_root and args.windows_tools_root.is_dir():
            source_bin = args.windows_tools_root.resolve() / "bin"
            target_bin = destination / "private" / "windows-tools" / "bin"
            required = (source_bin / "ffmpeg.exe", source_bin / "ffprobe.exe")
            if all(path.is_file() for path in required):
                target_bin.mkdir(parents=True, exist_ok=True)
                for path in (*required, *source_bin.glob("*.dll")):
                    shutil.copy2(path, target_bin / path.name)
                tools_included = True

        for name in (
            "README-FIRST.md",
            "INSTALL-AND-START-RADIOTEDU-MAC.command",
            "START-RADIOTEDU-MAC.command",
            "INSTALL-AND-START-RADIOTEDU-WINDOWS.bat",
        ):
            source = repository / "portable" / name
            if source.is_file():
                shutil.copy2(source, destination / name)

        commit = _git_value(repository, "rev-parse", "HEAD")
        files = []
        for path in sorted(destination.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                files.append(
                    {
                        "path": path.relative_to(destination).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
        manifest = {
            "schema_version": 1,
            "created_at": created_at,
            "source_commit": commit,
            "source_file_count": source_count,
            "archive_password_not_stored": True,
            "private_secrets_encrypted": True,
            "database_integrity_check": "ok",
            "credential_count": len(credentials),
            "station_count": len(secret_streams),
            "windows_ffmpeg_tools_included": tools_included,
            "files": files,
        }
        _write_json(destination / "manifest.json", manifest)
        print(
            json.dumps(
                {
                    "ok": True,
                    "destination": str(destination),
                    "source_commit": commit[:12],
                    "source_files": source_count,
                    "stations": len(secret_streams),
                    "credentials": len(credentials),
                    "database_integrity_check": "ok",
                    "windows_tools_included": tools_included,
                },
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
