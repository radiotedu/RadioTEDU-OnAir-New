"""Refresh the operator Desktop play-history CSV mirror.

Frequent runs refresh only recent daily reports. The nightly backup opts into
the full all-time mirror and hash-chain verification. The SQLite
music_usage_log remains append-only and authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export RadioTEDU play history CSVs")
    parser.add_argument("--db-path", default="", help="Absolute cleanroom.db path")
    parser.add_argument(
        "--history-root",
        default="",
        help="Desktop RadioTEDU Play History directory",
    )
    parser.add_argument(
        "--include-all-time",
        action="store_true",
        help="Refresh the full ledger mirror and verify its hash chain",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if str(args.db_path or "").strip():
        os.environ["CLEANROOM_DB_PATH"] = str(Path(args.db_path).expanduser().resolve())
    if str(args.history_root or "").strip():
        os.environ["RADIOTEDU_PLAY_HISTORY_ROOT"] = str(
            Path(args.history_root).expanduser().resolve()
        )

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Imports happen after environment selection because app.config resolves
    # the database path at import time.
    from app.services.music_usage import (
        MusicUsageService,
        open_music_usage_export_connection,
    )

    # This is a read/export job, not an application bootstrap. Running schema
    # initialization here races station workers and managed-folder writers on
    # the live SQLite file; the database is initialized by the service itself.
    conn = open_music_usage_export_connection()
    try:
        result = MusicUsageService(conn).ensure_daily_exports(
            include_all_time=bool(args.include_all_time)
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
