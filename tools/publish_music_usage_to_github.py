from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_DB = Path(r"C:\ProgramData\RadioTEDU\OnAir\cleanroom.db")
DEFAULT_REPO = Path(r"C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio")
TIMEZONE = ZoneInfo("Europe/Istanbul")
MOUNTS = {1: "/classic", 2: "/lofi", 4: "/radio", 5: "/cazz", 8: "/rock", 9: "/energize"}
FIELDS = (
    "report_date",
    "station_id",
    "station_name",
    "mount",
    "track_id",
    "work_title",
    "performer",
    "composer",
    "label",
    "isrc",
    "source_path",
    "play_count",
    "total_played_seconds",
    "first_broadcast_at",
    "last_broadcast_at",
)


def _rows(conn: sqlite3.Connection, report_date: str | None) -> list[dict]:
    where = "WHERE date(u.broadcast_at)=?" if report_date else ""
    params = (report_date,) if report_date else ()
    query = f"""
        SELECT u.station_id, COALESCE(s.name, '') AS station_name,
               COALESCE(u.track_id, 0) AS track_id,
               COALESCE(u.work_title, '') AS work_title,
               COALESCE(u.performer, '') AS performer,
               COALESCE(u.composer, '') AS composer,
               COALESCE(u.label, '') AS label,
               COALESCE(u.isrc, '') AS isrc,
               COALESCE(u.source_path, '') AS source_path,
               SUM(CASE WHEN COALESCE(u.publication_count, 0)>0
                        THEN u.publication_count ELSE 1 END) AS play_count,
               ROUND(SUM(COALESCE(u.played_duration_seconds, 0)), 3) AS total_played_seconds,
               MIN(u.broadcast_at) AS first_broadcast_at,
               MAX(u.broadcast_at) AS last_broadcast_at
        FROM music_usage_log u
        LEFT JOIN stations s ON s.id=u.station_id
        {where}
        GROUP BY u.station_id, u.track_id, u.work_title, u.performer, u.composer,
                 u.label, u.isrc, u.source_path
        ORDER BY u.station_id, play_count DESC, u.work_title COLLATE NOCASE,
                 u.performer COLLATE NOCASE, u.track_id
    """
    output = []
    for row in conn.execute(query, params):
        station_id = int(row["station_id"] or 0)
        output.append(
            {
                "report_date": report_date or "cumulative",
                "station_id": station_id,
                "station_name": str(row["station_name"] or ""),
                "mount": MOUNTS.get(station_id, ""),
                "track_id": int(row["track_id"] or 0),
                "work_title": str(row["work_title"] or ""),
                "performer": str(row["performer"] or ""),
                "composer": str(row["composer"] or ""),
                "label": str(row["label"] or ""),
                "isrc": str(row["isrc"] or ""),
                "source_path": str(row["source_path"] or ""),
                "play_count": int(row["play_count"] or 0),
                "total_played_seconds": float(row["total_played_seconds"] or 0),
                "first_broadcast_at": str(row["first_broadcast_at"] or ""),
                "last_broadcast_at": str(row["last_broadcast_at"] or ""),
            }
        )
    return output


def _atomic_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=environment,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish official nightly play-count CSVs to GitHub.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--date", default="", help="YYYY-MM-DD; defaults to yesterday in Europe/Istanbul")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--no-git", action="store_true", help="Write and validate CSV files without committing")
    args = parser.parse_args()
    report_date = args.date or str(datetime.now(TIMEZONE).date() - timedelta(days=1))
    datetime.strptime(report_date, "%Y-%m-%d")

    conn = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
            raise RuntimeError("live database integrity check failed")
        daily_rows = _rows(conn, report_date)
        cumulative_rows = _rows(conn, None)
    finally:
        conn.close()

    report_root = args.repo.resolve() / "reports" / "music-usage"
    daily_path = report_root / "daily" / f"{report_date}.csv"
    cumulative_path = report_root / "cumulative.csv"
    _atomic_csv(daily_path, daily_rows)
    _atomic_csv(cumulative_path, cumulative_rows)

    relative_paths = [
        str(daily_path.relative_to(args.repo)).replace("\\", "/"),
        str(cumulative_path.relative_to(args.repo)).replace("\\", "/"),
    ]
    changed = False
    if not args.no_git:
        _git(args.repo, "add", "--", *relative_paths)
        changed = _git(args.repo, "diff", "--cached", "--quiet", "--", *relative_paths, check=False).returncode != 0
        if changed:
            _git(args.repo, "commit", "-m", f"reports: publish RadioTEDU usage for {report_date}", "--", *relative_paths)
        if args.push:
            _git(args.repo, "push", "origin", "HEAD")
    print(
        f"date={report_date} daily_rows={len(daily_rows)} cumulative_rows={len(cumulative_rows)} "
        f"committed={str(changed).lower()} pushed={str(bool(args.push and not args.no_git)).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
