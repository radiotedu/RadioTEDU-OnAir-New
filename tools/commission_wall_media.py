from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.legacy import (  # noqa: E402
    LibraryFolderSyncPayload,
    sync_station_library_folder,
)


DEFAULT_SWEEPER_INTERVAL = 2
MIN_SWEEPER_INTERVAL = 1
MAX_SWEEPER_INTERVAL = 50


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _snapshot_database(source: Path, target: Path) -> None:
    source_conn = _connect(source)
    target_conn = _connect(target)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()


def _remove_pending_references(
    conn: sqlite3.Connection,
    station_id: int,
    track_ids: list[int],
) -> dict[str, int]:
    removed = {"queue": 0, "program": 0, "schedule": 0}
    for start in range(0, len(track_ids), 500):
        batch = track_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        cursor = conn.execute(
            f"DELETE FROM queue_items WHERE station_id=? AND status='pending' "
            f"AND track_id IN ({placeholders})",
            (station_id, *batch),
        )
        removed["queue"] += int(cursor.rowcount or 0)
        cursor = conn.execute(
            f"DELETE FROM program_queue_items WHERE station_id=? "
            f"AND track_id IN ({placeholders})",
            (station_id, *batch),
        )
        removed["program"] += int(cursor.rowcount or 0)
        cursor = conn.execute(
            f"DELETE FROM schedule_items WHERE station_id=? AND status='pending' "
            f"AND track_id IN ({placeholders})",
            (station_id, *batch),
        )
        removed["schedule"] += int(cursor.rowcount or 0)
    return removed


def _deactivate_missing_media(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id, station_id, track_type, file_path FROM tracks "
        "WHERE is_active=1 ORDER BY station_id, id"
    ).fetchall()
    missing_by_station: dict[int, list[int]] = {}
    missing_by_type: dict[str, int] = {}
    for row in rows:
        file_path = str(row["file_path"] or "").strip()
        if not file_path or Path(file_path).is_file():
            continue
        station_id = int(row["station_id"])
        missing_by_station.setdefault(station_id, []).append(int(row["id"]))
        kind = str(row["track_type"] or "unknown").strip().lower()
        missing_by_type[kind] = missing_by_type.get(kind, 0) + 1

    removed = {"queue": 0, "program": 0, "schedule": 0}
    for station_id, track_ids in missing_by_station.items():
        for start in range(0, len(track_ids), 500):
            batch = track_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            conn.execute(
                f"UPDATE tracks SET is_active=0 WHERE id IN ({placeholders})",
                tuple(batch),
            )
        station_removed = _remove_pending_references(conn, station_id, track_ids)
        for key, value in station_removed.items():
            removed[key] += value

    return {
        "deactivated": sum(len(value) for value in missing_by_station.values()),
        "by_station": {
            str(station_id): len(track_ids)
            for station_id, track_ids in sorted(missing_by_station.items())
        },
        "by_type": dict(sorted(missing_by_type.items())),
        "pending_references_removed": removed,
    }


def _ensure_jingle_intervals(conn: sqlite3.Connection) -> dict[str, int]:
    inserted = 0
    normalized = 0
    stations = conn.execute("SELECT id FROM stations ORDER BY id").fetchall()
    for station in stations:
        station_id = int(station["id"])
        row = conn.execute(
            "SELECT value FROM station_settings "
            "WHERE station_id=? AND key='sweeper_interval'",
            (station_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO station_settings (station_id, key, value, updated_at) "
                "VALUES (?, 'sweeper_interval', ?, CURRENT_TIMESTAMP)",
                (station_id, str(DEFAULT_SWEEPER_INTERVAL)),
            )
            inserted += 1
            continue
        try:
            value = int(str(row["value"] or "").strip())
        except (TypeError, ValueError):
            value = DEFAULT_SWEEPER_INTERVAL
        safe_value = max(MIN_SWEEPER_INTERVAL, min(MAX_SWEEPER_INTERVAL, value))
        if safe_value != value or str(row["value"]) != str(safe_value):
            conn.execute(
                "UPDATE station_settings SET value=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE station_id=? AND key='sweeper_interval'",
                (str(safe_value), station_id),
            )
            normalized += 1
    return {"inserted": inserted, "normalized": normalized}


def _validation_snapshot(conn: sqlite3.Connection) -> dict:
    quick_check = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
    stations = []
    errors: list[str] = []
    enabled_mounts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT s.id, s.name, o.icecast_enabled, o.icecast_host, "
        "o.icecast_port, o.icecast_mount "
        "FROM stations s LEFT JOIN station_outputs o ON o.station_id=s.id "
        "ORDER BY s.id"
    ):
        station_id = int(row["id"])
        music = int(
            conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE station_id=? "
                "AND is_active=1 AND LOWER(track_type)='music'",
                (station_id,),
            ).fetchone()[0]
        )
        jingles = int(
            conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE station_id=? "
                "AND is_active=1 AND LOWER(track_type)='jingle'",
                (station_id,),
            ).fetchone()[0]
        )
        interval_row = conn.execute(
            "SELECT value FROM station_settings "
            "WHERE station_id=? AND key='sweeper_interval'",
            (station_id,),
        ).fetchone()
        interval = int(
            str(
                interval_row["value"]
                if interval_row is not None
                else DEFAULT_SWEEPER_INTERVAL
            )
        )
        mount = str(row["icecast_mount"] or "").strip()
        enabled = bool(row["icecast_enabled"])
        if enabled:
            if not mount.startswith("/"):
                errors.append(f"station {station_id} has an invalid mount")
            if not str(row["icecast_host"] or "").strip():
                errors.append(f"station {station_id} has no Icecast host")
            if mount in enabled_mounts:
                errors.append(
                    f"stations {enabled_mounts[mount]} and {station_id} share mount {mount}"
                )
            enabled_mounts[mount] = station_id
        if music == 0:
            errors.append(f"station {station_id} has no active music")
        if jingles == 0:
            errors.append(f"station {station_id} has no active jingles")
        stations.append(
            {
                "station_id": station_id,
                "name": str(row["name"] or ""),
                "music": music,
                "jingles": jingles,
                "sweeper_interval": interval,
                "icecast_enabled": enabled,
                "mount": mount,
            }
        )
    if quick_check.lower() != "ok":
        errors.append(f"SQLite quick_check failed: {quick_check}")
    return {
        "ok": not errors,
        "quick_check": quick_check,
        "errors": errors,
        "stations": stations,
    }


def commission(
    target_db: Path,
    *,
    lofi_folder: Path | None,
    apply: bool,
) -> dict:
    target_db = target_db.expanduser().resolve()
    if not target_db.is_file():
        raise FileNotFoundError(f"Target database not found: {target_db}")

    staging = target_db.with_name(
        f".{target_db.name}.commission-{uuid.uuid4().hex}.tmp"
    )
    backup: Path | None = None
    original_db_path = os.environ.get("CLEANROOM_DB_PATH")
    try:
        _snapshot_database(target_db, staging)
        os.environ["CLEANROOM_DB_PATH"] = str(staging)
        folder_sync = None
        if lofi_folder is not None:
            lofi_folder = lofi_folder.expanduser().resolve()
            folder_sync = sync_station_library_folder(
                LibraryFolderSyncPayload(
                    station_id=2,
                    folder=str(lofi_folder),
                    recursive=True,
                    track_type="music",
                    mode="merge",
                    skip_unplayable=True,
                    remove_pending_queue=True,
                    profile_label="RadioTEDU Lo-Fi",
                    default_genre="Lo-Fi",
                )
            )

        conn = _connect(staging)
        try:
            missing = _deactivate_missing_media(conn)
            intervals = _ensure_jingle_intervals(conn)
            conn.commit()
            validation = _validation_snapshot(conn)
        finally:
            conn.close()

        summary = {
            "dry_run": not apply,
            "target_database": str(target_db),
            "target_replaced": False,
            "backup_database": "",
            "lofi_folder_sync": folder_sync,
            "missing_media": missing,
            "jingle_intervals": intervals,
            "validation": validation,
        }
        if not validation["ok"]:
            raise RuntimeError(
                "Commissioning validation failed: "
                + "; ".join(validation["errors"])
            )
        if not apply:
            return summary

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target_db.with_name(
            f"{target_db.name}.backup-before-media-{timestamp}"
        )
        _snapshot_database(target_db, backup)
        os.replace(staging, target_db)
        summary["target_replaced"] = True
        summary["backup_database"] = str(backup)
        return summary
    finally:
        if original_db_path is None:
            os.environ.pop("CLEANROOM_DB_PATH", None)
        else:
            os.environ["CLEANROOM_DB_PATH"] = original_db_path
        staging.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Commission migrated Broadcast Wall media on a staging database."
    )
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--lofi-folder", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        summary = commission(
            Path(args.target_db),
            lofi_folder=Path(args.lofi_folder)
            if str(args.lofi_folder).strip()
            else None,
            apply=bool(args.apply),
        )
    except HTTPException as exc:
        detail = exc.detail
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "library_sync_failed",
                    "status_code": exc.status_code,
                    "detail": detail,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 1
    print(json.dumps({"ok": True, **summary}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
