"""Export RadioTEDU's immutable play ledger into operator-friendly reports.

This command is intentionally stdlib-only and safe to run beside live playout.
It takes a consistent SQLite read snapshot, validates every per-station hash
chain, and atomically replaces only the rolling ``current`` reports.  Daily and
monthly exports use period-specific names so an official archive is retained.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Path to the live cleanroom.db")
    parser.add_argument("--output", required=True, help="Official report directory")
    return parser.parse_args()


def _acquire_lock(root: Path) -> tuple[int, Path] | None:
    lock_path = root / ".music-usage-export.lock"
    root.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime <= 3600:
                return None
            lock_path.unlink()
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileNotFoundError, FileExistsError, OSError):
            return None
    os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
    return descriptor, lock_path


def _release_lock(lock: tuple[int, Path] | None) -> None:
    if lock is None:
        return
    descriptor, lock_path = lock
    try:
        os.close(descriptor)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    args = _arguments()
    database = Path(args.db).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"RadioTEDU database not found: {database}")
    lock = _acquire_lock(output)
    if lock is None:
        print(json.dumps({"ok": True, "status": "already_running"}))
        return 0

    try:
        repository_root = Path(__file__).resolve().parents[1]
        if str(repository_root) not in sys.path:
            sys.path.insert(0, str(repository_root))
        from app.services.music_usage import MusicUsageService

        conn = sqlite3.connect(database, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN")
            service = MusicUsageService(conn)
            integrity = service.verify_hash_chain()
            if not bool(integrity.get("valid")):
                failure = {
                    "generated_at_utc": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "integrity": integrity,
                }
                service._atomic_write_text(
                    output / "RadioTEDU-music-usage-integrity-FAILED.json",
                    json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                )
                conn.rollback()
                print(json.dumps({"ok": False, **failure}, separators=(",", ":")))
                return 2

            current = service.export_official_current(destination=output / "current")
            today = datetime.now(timezone.utc).date()
            previous = today - timedelta(days=1)
            daily = service.export_csv(
                destination=output / "daily" / f"{previous.isoformat()}.csv",
                date_from=previous.isoformat(),
                date_to=today.isoformat(),
            )
            conn.commit()

            monthly = None
            if today.day == 1:
                monthly = service.close_month(
                    year=previous.year,
                    month=previous.month,
                    closed_by="RadioTEDU official export task",
                    export_path=(
                        output
                        / "monthly"
                        / f"{previous.year:04d}-{previous.month:02d}.csv"
                    ),
                )
            summary = {
                "ok": True,
                "generated_at_utc": current["generated_at_utc"],
                "integrity_valid": True,
                "event_count": current["events"]["record_count"],
                "play_count_rows": current["play_counts"]["record_count"],
                "daily_records": daily["record_count"],
                "monthly_closed": bool(monthly),
                "output": str(output),
            }
            print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
            return 0
        finally:
            conn.close()
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
