"""Persist the RadioTEDU ITU-R BS.1770 / EBU R128 station policy."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


PROFILE = "itu_bs1770"
LOUDNESS_TARGET_LUFS = "-23.0"


def apply_settings(database_path: Path) -> dict:
    connection = sqlite3.connect(str(database_path), timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        stations = list(connection.execute("SELECT id, name FROM stations ORDER BY id"))
        for key, value in (
            ("broadcast_processing_profile", PROFILE),
            ("loudness_target_lufs", LOUDNESS_TARGET_LUFS),
        ):
            connection.execute(
                "INSERT INTO system_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )
        for station_id, _station_name in stations:
            for key, value in (
                ("broadcast_processing_profile", PROFILE),
                ("loudness_target_lufs", LOUDNESS_TARGET_LUFS),
            ):
                connection.execute(
                    "INSERT INTO station_settings "
                    "(station_id, key, value, updated_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(station_id, key) DO UPDATE SET "
                    "value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (int(station_id), key, value),
                )
        connection.commit()
        return {
            "ok": True,
            "profile": PROFILE,
            "loudness_target_lufs": float(LOUDNESS_TARGET_LUFS),
            "stations": [
                {"id": int(station_id), "name": str(name)}
                for station_id, name in stations
            ],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    result = apply_settings(args.db.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
