"""Export and close RadioTEDU music-use records for scheduled operations."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_connection, init_db
from app.runtime_paths import get_data_dir
from app.services.music_usage import MusicUsageService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", dest="day", help="UTC day (YYYY-MM-DD); defaults to yesterday")
    parser.add_argument("--month", help="Close month (YYYY-MM)")
    parser.add_argument("--output", help="CSV output path for --date")
    parser.add_argument("--closed-by", default="scheduled-task")
    args = parser.parse_args()
    init_db()
    conn = get_connection()
    try:
        service = MusicUsageService(conn)
        if args.month:
            year_text, month_text = str(args.month).split("-", 1)
            result = service.close_month(
                year=int(year_text), month=int(month_text), closed_by=args.closed_by
            )
            print(f"closed_month={result['period_key']} records={result['record_count']} checksum={result['checksum']}")
            return 0
        day = date.fromisoformat(args.day) if args.day else date.today() - timedelta(days=1)
        output = args.output or str(get_data_dir() / "Exports" / "MusicUsage" / f"{day.isoformat()}.csv")
        result = service.export_csv(
            destination=output,
            date_from=day.isoformat(),
            date_to=(day + timedelta(days=1)).isoformat(),
        )
        print(f"exported={result['path']} records={result['record_count']} checksum={result['checksum']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
