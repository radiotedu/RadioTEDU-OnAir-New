"""Read-only validation of every active RadioTEDU OnAir media file."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.legacy import _get_audio_metadata
from app.reliability import atomic_write_json


def _validate(row: tuple[int, int, str, str, str]) -> dict[str, object]:
    track_id, station_id, track_type, title, file_path = row
    path = Path(file_path)
    result: dict[str, object] = {
        "track_id": track_id,
        "station_id": station_id,
        "track_type": track_type,
        "title": title,
        "file_path": str(path),
        "ok": False,
    }
    if not path.is_file():
        result["error"] = "missing"
        return result
    try:
        metadata = _get_audio_metadata(
            str(path),
            fallback_title=path.stem or title or "Track",
            require_playable=True,
        )
        result["ok"] = True
        result["duration"] = float(metadata.get("duration") or 0.0)
    except Exception as exc:  # noqa: BLE001 - this is an audit boundary
        result["error"] = str(exc or "unplayable")[:500]
    return result


def validate(db_path: Path, workers: int = 4) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, station_id, track_type, title, file_path "
            "FROM tracks WHERE is_active=1 ORDER BY station_id, track_type, id"
        ).fetchall()
    unique_rows: dict[str, tuple[int, int, str, str, str]] = {}
    for raw in rows:
        row = (
            int(raw[0]),
            int(raw[1]),
            str(raw[2] or "music"),
            str(raw[3] or ""),
            str(raw[4] or ""),
        )
        unique_rows.setdefault(str(Path(row[4])).casefold(), row)

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
        results = list(pool.map(_validate, unique_rows.values()))

    failures = [item for item in results if not item["ok"]]
    groups = Counter(
        f"{int(item['station_id'])}:{item['track_type']}"
        for item in results
        if item["ok"]
    )
    return {
        "ok": not failures,
        "database": str(db_path.resolve()),
        "active_rows": len(rows),
        "unique_files": len(results),
        "playable_files": len(results) - len(failures),
        "failed_files": len(failures),
        "playable_by_station_and_type": dict(sorted(groups.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    report = validate(Path(args.db), workers=args.workers)
    atomic_write_json(Path(args.report), report)
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "active_rows": report["active_rows"],
                "unique_files": report["unique_files"],
                "playable_files": report["playable_files"],
                "failed_files": report["failed_files"],
                "report": str(Path(args.report).resolve()),
            },
            ensure_ascii=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
