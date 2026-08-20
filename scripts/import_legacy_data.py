import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_db_path
from app.migration.legacy_import import migrate_legacy_database_safely


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import legacy radio automation data into cleanroom.",
    )
    parser.add_argument(
        "--legacy-db",
        required=True,
        help="Path to the legacy SQLite database.",
    )
    parser.add_argument(
        "--cleanroom-db",
        default=str(get_db_path()),
        help="Target cleanroom SQLite database path.",
    )
    parser.add_argument(
        "--copy-media",
        action="store_true",
        help="Copy media only when --apply is also supplied.",
    )
    parser.add_argument(
        "--media-destination-root",
        default="",
        help="Optional override for the cleanroom media destination root.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Atomically replace the target after staging and validation. "
            "Without this flag the command is a non-destructive dry run."
        ),
    )
    args = parser.parse_args()

    summary = migrate_legacy_database_safely(
        legacy_db_path=Path(args.legacy_db),
        cleanroom_db_path=Path(args.cleanroom_db),
        dry_run=not bool(args.apply),
        copy_media=bool(args.copy_media),
        media_destination_root=Path(args.media_destination_root)
        if str(args.media_destination_root).strip()
        else None,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
