"""Create a consistent, access-restricted-ready snapshot of RadioTEDU state.

The script never reads secret values into stdout. It copies credential/configuration
files byte-for-byte, uses SQLite's online backup API for the live database, and
writes a SHA-256 manifest so the snapshot can be verified before migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


PRIMARY_ROOT = Path(r"C:\ProgramData\RadioTEDU\OnAir")
SOURCE_ROOTS = (
    ("programdata-onair", PRIMARY_ROOT),
    ("programdata-shared-secrets", Path(r"C:\ProgramData\RadioTEDU\secrets")),
    ("programdata-ai-config", Path(r"C:\ProgramData\RadioTEDU\ai-broadcast-agent\config")),
    ("operator-onair", Path(os.environ.get("LOCALAPPDATA", "")) / "RadioTEDU" / "OnAir"),
    ("legacy-wall-service", Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "RadioTEDU Broadcast Wall" / "service"),
)
EXPLICIT_FILES = (
    ("voting-agent-env", PRIMARY_ROOT / "services" / "radiotedu-voting" / "tools" / "local-voting-agent" / ".env"),
    ("jukebox-agent-env", PRIMARY_ROOT / "services" / "radiotedu-jukebox" / "media-agent" / ".env"),
)

FULL_DIRECTORIES = {
    "control",
    "state",
    "secrets",
    "radiotedu-services",
    "service-backups",
    "exports",
}
EXACT_FILES = {
    ".env",
    "guard-child-env.txt",
    "guard-child-env.txt.bak-20260802-0204",
    "initial-admin-password.txt",
    "jwt-secret.key",
    "station-credentials.json",
    "supervisor-config.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".pfx", ".p12", ".crt", ".cer"}
CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".services"}
CONFIG_WORDS = ("config", "credential", "secret", "setting", "service", "supervisor", "token")
SKIP_DIRECTORIES = {"recovery", "recovery-points", "schema-backups", "backups", "commissioning", "logs", "media", "uploads", "ai_cache"}
TRANSIENT_DIRECTORIES = {"stationworkers"}
PRIMARY_ALLOWED_DIRECTORIES = {"control", "state", "secrets", "radiotedu-services", "service-backups", "exports"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_selected(relative: Path) -> bool:
    parts = [part.casefold() for part in relative.parts]
    name = relative.name.casefold()
    if any(part in TRANSIENT_DIRECTORIES for part in parts[:-1]):
        return False
    if name.endswith(".tmp") or ".heartbeat.json." in name:
        return False
    if any(part in SKIP_DIRECTORIES for part in parts[:-1]):
        return False
    if name.startswith("tsconfig") or name in {"package.json", "package-lock.json"}:
        return False
    if any(part in FULL_DIRECTORIES for part in parts[:-1]):
        return True
    if name in EXACT_FILES or name.startswith(".env.") or name.endswith(".env"):
        return True
    if relative.suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    if name.endswith(".services"):
        return True
    return relative.suffix.casefold() in CONFIG_SUFFIXES and any(word in name for word in CONFIG_WORDS)


def _selected_source_files(label: str, source: Path):
    if not source.is_dir():
        return
    for current, directories, filenames in os.walk(source):
        current_path = Path(current)
        if label == "programdata-onair" and current_path == source:
            directories[:] = [directory for directory in directories if directory.casefold() in PRIMARY_ALLOWED_DIRECTORIES]
        else:
            directories[:] = [directory for directory in directories if directory.casefold() not in SKIP_DIRECTORIES]
        for filename in filenames:
            item = current_path / filename
            if item.is_symlink():
                continue
            relative = item.relative_to(source)
            if label == "programdata-onair" and current_path == source:
                selected = filename.casefold() in EXACT_FILES
            else:
                selected = is_selected(relative)
            if selected:
                yield item, relative


def _ensure_readable(source: Path) -> None:
    with source.open("rb"):
        return


def copy_selected(label: str, source: Path, destination: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for item, relative in _selected_source_files(label, source):
        target = destination / "files" / label / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, target)
        except FileNotFoundError:
            # Worker heartbeat/status files are atomically replaced. A name
            # disappearing between enumeration and copy is normal and is not
            # part of the durable configuration snapshot.
            continue
        records.append(
            {
                "source_group": label,
                "relative_path": relative.as_posix(),
                "snapshot_path": target.relative_to(destination).as_posix(),
                "size": target.stat().st_size,
                "sha256": sha256(target),
            }
        )
    return records


def copy_explicit(label: str, source: Path, destination: Path) -> dict[str, object] | None:
    if not source.is_file():
        return None
    target = destination / "files" / "explicit" / label / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {
        "source_group": label,
        "relative_path": source.name,
        "snapshot_path": target.relative_to(destination).as_posix(),
        "size": target.stat().st_size,
        "sha256": sha256(target),
    }


def backup_database(destination: Path) -> dict[str, object]:
    source = PRIMARY_ROOT / "cleanroom.db"
    if not source.is_file():
        raise FileNotFoundError(f"Live database not found: {source}")
    target = destination / "database" / "cleanroom.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("Snapshot database failed SQLite integrity_check")
    return {
        "source_group": "programdata-onair",
        "relative_path": "cleanroom.db",
        "snapshot_path": target.relative_to(destination).as_posix(),
        "size": target.stat().st_size,
        "sha256": sha256(target),
        "sqlite_integrity_check": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    recovery_root = (PRIMARY_ROOT / "Recovery").resolve()
    if destination == recovery_root or recovery_root not in destination.parents:
        raise ValueError("Destination must be a new child of the canonical OnAir Recovery directory")
    if destination.exists():
        raise ValueError("Destination must be a new child of the canonical OnAir Recovery directory")

    # Fail before creating any staging tree when a protected source is not
    # readable. This keeps ACL failures from producing ambiguous partial
    # snapshots containing only some credentials.
    try:
        for label, source in SOURCE_ROOTS:
            for item, _relative in _selected_source_files(label, source):
                try:
                    _ensure_readable(item)
                except FileNotFoundError:
                    continue
        for _label, source in EXPLICIT_FILES:
            if source.is_file():
                _ensure_readable(source)
    except PermissionError as exc:
        raise PermissionError(
            "A protected RadioTEDU state or credential file could not be read; "
            "run this backup from an elevated account without weakening its ACL."
        ) from exc

    staging = recovery_root / f".{destination.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        records = [backup_database(staging)]
        for label, source in SOURCE_ROOTS:
            records.extend(copy_selected(label, source, staging))
        for label, source in EXPLICIT_FILES:
            record = copy_explicit(label, source, staging)
            if record:
                records.append(record)

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "pre-consolidation RadioTEDU OnAir state and credential snapshot",
            "secret_values_logged": False,
            "database_integrity_check": "ok",
            "file_count": len(records),
            "total_bytes": sum(int(record["size"]) for record in records),
            "files": sorted(records, key=lambda record: (str(record["source_group"]), str(record["relative_path"]))),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        try:
            staging.replace(destination)
        except PermissionError:
            # Some hardened ProgramData ACLs allow creating recovery children
            # but intentionally deny directory rename/delete. Copy the fully
            # built snapshot into its final new directory in that case.
            shutil.copytree(staging, destination)
        print(json.dumps({"destination": str(destination), "file_count": len(records), "database_integrity_check": "ok"}))
        return 0
    except PermissionError as exc:
        raise PermissionError(
            "A protected RadioTEDU state or credential file could not be read; "
            "run this backup from an elevated account without weakening its ACL."
        ) from exc
    finally:
        for attempt in range(8):
            if not staging.exists():
                break
            shutil.rmtree(staging, ignore_errors=True)
            if staging.exists():
                time.sleep(0.05 * (attempt + 1))


if __name__ == "__main__":
    raise SystemExit(main())
