#!/usr/bin/env python3
"""Read-only structural audit of a Broadcast Wall to OnAir migration.

The report deliberately omits credentials and emits aggregate track/media
information only. It is safe to retain with commissioning evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


_OUTPUT_SETTING_KEYS = (
    "output_mode",
    "speaker_monitor_enabled",
    "output_device_id",
    "icecast_enabled",
    "icecast_host",
    "icecast_port",
    "icecast_mount",
    "icecast_username",
    "icecast_user",
    "output_gain_db",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _stations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"id": int(row["id"]), "name": str(row["name"] or "")}
        for row in conn.execute("SELECT id, name FROM stations ORDER BY id")
    ]


def _track_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT COALESCE(station_id, -1) AS station_id, "
        "LOWER(COALESCE(track_type, 'music')) AS track_type, "
        "CASE WHEN COALESCE(is_active, 1) <> 0 THEN 1 ELSE 0 END AS active, "
        "COUNT(*) AS count "
        "FROM tracks GROUP BY station_id, track_type, active "
        "ORDER BY station_id, track_type, active"
    ).fetchall()
    return [
        {
            "station_id": int(row["station_id"]),
            "track_type": str(row["track_type"]),
            "active": bool(row["active"]),
            "count": int(row["count"]),
        }
        for row in rows
    ]


def _orphan_track_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    station_ids = {int(row["id"]) for row in conn.execute("SELECT id FROM stations")}
    return [
        row
        for row in _track_groups(conn)
        if int(row["station_id"]) not in station_ids
    ]


def _missing_active_media(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    for row in conn.execute(
        "SELECT station_id, file_path FROM tracks "
        "WHERE COALESCE(is_active, 1) <> 0"
    ):
        file_path = str(row["file_path"] or "").strip()
        if not file_path or not os.path.isfile(file_path):
            sid = int(row["station_id"] or -1)
            counts[sid] = counts.get(sid, 0) + 1
    return [
        {"station_id": sid, "missing_active_media": count}
        for sid, count in sorted(counts.items())
    ]


def _source_output_settings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in _OUTPUT_SETTING_KEYS)
    rows = conn.execute(
        f"SELECT station_id, key, value FROM station_settings "
        f"WHERE key IN ({placeholders}) ORDER BY station_id, key",
        _OUTPUT_SETTING_KEYS,
    ).fetchall()
    grouped: dict[int, dict[str, str]] = {}
    for row in rows:
        grouped.setdefault(int(row["station_id"]), {})[str(row["key"])] = str(
            row["value"] or ""
        )
    return [
        {"station_id": sid, **values}
        for sid, values in sorted(grouped.items())
    ]


def _target_outputs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "station_outputs"):
        return []
    allowed = (
        "station_id",
        "local_output_enabled",
        "output_device_id",
        "icecast_enabled",
        "icecast_host",
        "icecast_port",
        "icecast_mount",
        "icecast_user",
        "output_gain_db",
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(station_outputs)")
    }
    selected = [name for name in allowed if name in columns]
    rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM station_outputs ORDER BY station_id"
    ).fetchall()
    return [{key: row[key] for key in selected} for row in rows]


def audit(source: Path, target: Path) -> dict[str, Any]:
    source_conn = _connect(source)
    target_conn = _connect(target)
    try:
        return {
            "ok": True,
            "source": {
                "path": str(source.resolve()),
                "quick_check": source_conn.execute("PRAGMA quick_check").fetchone()[0],
                "stations": _stations(source_conn),
                "track_groups": _track_groups(source_conn),
                "orphan_track_groups": _orphan_track_groups(source_conn),
                "missing_active_media": _missing_active_media(source_conn),
                "output_settings": _source_output_settings(source_conn),
            },
            "target": {
                "path": str(target.resolve()),
                "quick_check": target_conn.execute("PRAGMA quick_check").fetchone()[0],
                "stations": _stations(target_conn),
                "track_groups": _track_groups(target_conn),
                "orphan_track_groups": _orphan_track_groups(target_conn),
                "missing_active_media": _missing_active_media(target_conn),
                "outputs": _target_outputs(target_conn),
            },
        }
    finally:
        source_conn.close()
        target_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    args = parser.parse_args()
    report = audit(Path(args.source_db), Path(args.target_db))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
