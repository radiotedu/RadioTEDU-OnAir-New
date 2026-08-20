"""Permanent music-use records for repertoire and rights reporting.

Every completed music play is written as an append-only, hash-chained row.  The
row contains a snapshot of the metadata at air time, so later library edits do
not rewrite the historical report.  CSV exports are deterministic and are
stored beneath the protected RadioTEDU data root for backup and monthly close.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from app.runtime_paths import get_data_dir

_log = logging.getLogger(__name__)

USAGE_COLUMNS = (
    "broadcast_at",
    "station_id",
    "track_id",
    "work_title",
    "version",
    "performer",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "played_duration_seconds",
    "publication_count",
    "source_path",
    "source_reference",
    "rights_reference",
    "program_name",
    "presenter",
    "delivered_variants_json",
    "log_id",
    "entry_hash",
)

PLAY_COUNT_COLUMNS = (
    "station_id",
    "station_name",
    "mount",
    "mount_status",
    "track_id",
    "work_title",
    "performer",
    "composer",
    "label",
    "isrc",
    "source_path",
    "completed_play_count",
    "event_count",
    "total_played_seconds",
    "first_broadcast_at",
    "last_broadcast_at",
)

HASH_PAYLOAD_COLUMNS = (
    "station_id",
    "queue_item_id",
    "track_id",
    "broadcast_at",
    "work_title",
    "version",
    "performer",
    "composer",
    "lyricist",
    "phonogram_producer",
    "label",
    "isrc",
    "scheduled_duration_seconds",
    "played_duration_seconds",
    "publication_count",
    "source_path",
    "source_reference",
    "rights_reference",
    "program_name",
    "presenter",
    "delivered_variants_json",
    "log_id",
)


def _text(value, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_timestamp(value) -> str:
    if value in (None, ""):
        return _utc_now().strftime("%Y-%m-%d %H:%M:%S")
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:32]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_seconds(started_at, finished_at, fallback: float) -> float:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0.0, round((end - start).total_seconds(), 3))
    except (TypeError, ValueError):
        return max(0.0, round(float(fallback or 0.0), 3))


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(int(year), int(month), 1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def _csv_safe(value) -> str:
    text = str(value if value is not None else "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _delivered_variants(value) -> list[dict]:
    normalized = []
    seen = set()
    for item in value or []:
        if not isinstance(item, dict):
            continue
        mount = _text(item.get("mount"), 240)
        if not mount:
            continue
        if not mount.startswith("/"):
            mount = f"/{mount}"
        if mount in seen:
            continue
        seen.add(mount)
        normalized.append(
            {
                "mount": mount,
                "quality": _text(item.get("quality"), 40),
                "codec_profile": _text(item.get("codec_profile"), 80),
                "bitrate_kbps": max(0, int(item.get("bitrate_kbps") or 0)),
            }
        )
    return sorted(normalized, key=lambda item: item["mount"])


class MusicUsageService:
    def __init__(self, conn):
        self.conn = conn

    def upsert_track_metadata(self, track_id: int, payload: dict) -> dict:
        values = {
            "version": _text(payload.get("version")),
            "composer": _text(payload.get("composer")),
            "lyricist": _text(payload.get("lyricist")),
            "phonogram_producer": _text(payload.get("phonogram_producer")),
            "label": _text(payload.get("label")),
            "isrc": _text(payload.get("isrc"), 80).upper(),
            "source_reference": _text(payload.get("source_reference")),
            "rights_reference": _text(payload.get("rights_reference")),
            "source_type": _text(payload.get("source_type"), 120),
            "notes": _text(payload.get("notes")),
        }
        self.conn.execute(
            "INSERT INTO track_broadcast_metadata "
            "(track_id, version, composer, lyricist, phonogram_producer, label, isrc, "
            "source_reference, rights_reference, source_type, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(track_id) DO UPDATE SET version=excluded.version, "
            "composer=excluded.composer, lyricist=excluded.lyricist, "
            "phonogram_producer=excluded.phonogram_producer, label=excluded.label, "
            "isrc=excluded.isrc, source_reference=excluded.source_reference, "
            "rights_reference=excluded.rights_reference, source_type=excluded.source_type, "
            "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
            (
                int(track_id),
                values["version"],
                values["composer"],
                values["lyricist"],
                values["phonogram_producer"],
                values["label"],
                values["isrc"],
                values["source_reference"],
                values["rights_reference"],
                values["source_type"],
                values["notes"],
            ),
        )
        self.conn.commit()
        return self.get_track_metadata(int(track_id)) or {"track_id": int(track_id), **values}

    def get_track_metadata(self, track_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT track_id, version, composer, lyricist, phonogram_producer, label, isrc, "
            "source_reference, rights_reference, source_type, notes, updated_at "
            "FROM track_broadcast_metadata WHERE track_id=?",
            (int(track_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_completed_play(
        self,
        *,
        station_id: int,
        track_id: int,
        queue_item_id: int | None = None,
        started_at=None,
        finished_at=None,
        program_name: str = "",
        presenter: str = "",
        delivered_variants: list[dict] | None = None,
        log_id: str | None = None,
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT t.id, t.station_id, t.title, t.artist, t.duration, t.track_type, t.file_path, "
            "m.version, m.composer, m.lyricist, m.phonogram_producer, m.label, m.isrc, "
            "m.source_reference, m.rights_reference "
            "FROM tracks t LEFT JOIN track_broadcast_metadata m ON m.track_id=t.id "
            "WHERE t.id=? LIMIT 1",
            (int(track_id),),
        ).fetchone()
        if row is None or str(row["track_type"] or "music").strip().lower() not in {"music", "jingle"}:
            return None
        stable_log_id = _text(log_id or (f"queue:{int(queue_item_id)}" if queue_item_id else f"track:{int(track_id)}:{_sqlite_timestamp(finished_at)}"), 160)
        existing = self.conn.execute(
            "SELECT * FROM music_usage_log WHERE log_id=? LIMIT 1", (stable_log_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        broadcast_at = _sqlite_timestamp(finished_at)
        scheduled = max(0.0, round(float(row["duration"] or 0.0), 3))
        played = _elapsed_seconds(started_at, finished_at, scheduled)
        metadata = {
            "version": _text(row["version"]),
            "composer": _text(row["composer"]),
            "lyricist": _text(row["lyricist"]),
            "phonogram_producer": _text(row["phonogram_producer"]),
            "label": _text(row["label"]),
            "isrc": _text(row["isrc"], 80).upper(),
            "source_reference": _text(row["source_reference"]),
            "rights_reference": _text(row["rights_reference"]),
        }
        normalized_variants = _delivered_variants(delivered_variants)
        payload = {
            "station_id": int(station_id),
            "queue_item_id": int(queue_item_id) if queue_item_id is not None else None,
            "track_id": int(track_id),
            "broadcast_at": broadcast_at,
            "work_title": _text(row["title"]),
            "version": metadata["version"],
            "performer": _text(row["artist"]),
            "composer": metadata["composer"],
            "lyricist": metadata["lyricist"],
            "phonogram_producer": metadata["phonogram_producer"],
            "label": metadata["label"],
            "isrc": metadata["isrc"],
            "scheduled_duration_seconds": scheduled,
            "played_duration_seconds": played,
            "publication_count": 1,
            "source_path": _text(row["file_path"], 4000),
            "source_reference": metadata["source_reference"],
            "rights_reference": metadata["rights_reference"],
            "program_name": _text(program_name),
            "presenter": _text(presenter),
            "delivered_variants_json": json.dumps(
                normalized_variants,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "log_id": stable_log_id,
        }
        previous = self.conn.execute(
            "SELECT entry_hash FROM music_usage_log WHERE station_id=? ORDER BY id DESC LIMIT 1",
            (int(station_id),),
        ).fetchone()
        previous_hash = _text(previous[0] if previous else "", 128)
        canonical = json.dumps({**payload, "previous_hash": previous_hash}, sort_keys=True, separators=(",", ":"))
        entry_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            self.conn.execute(
                "INSERT INTO music_usage_log "
                "(station_id, queue_item_id, track_id, broadcast_at, work_title, version, performer, composer, lyricist, "
                "phonogram_producer, label, isrc, scheduled_duration_seconds, played_duration_seconds, publication_count, "
                "source_path, source_reference, rights_reference, program_name, presenter, delivered_variants_json, "
                "log_id, metadata_snapshot_json, previous_hash, entry_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["station_id"], payload["queue_item_id"], payload["track_id"], payload["broadcast_at"],
                    payload["work_title"], payload["version"], payload["performer"], payload["composer"], payload["lyricist"],
                    payload["phonogram_producer"], payload["label"], payload["isrc"], payload["scheduled_duration_seconds"],
                    payload["played_duration_seconds"], payload["publication_count"], payload["source_path"],
                    payload["source_reference"], payload["rights_reference"], payload["program_name"], payload["presenter"],
                    payload["delivered_variants_json"], payload["log_id"], json.dumps(
                        {**metadata, "delivered_variants": normalized_variants},
                        ensure_ascii=False,
                        sort_keys=True,
                    ), previous_hash, entry_hash,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            existing = self.conn.execute(
                "SELECT * FROM music_usage_log WHERE log_id=? LIMIT 1", (stable_log_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            raise
        return dict(self.conn.execute("SELECT * FROM music_usage_log WHERE log_id=?", (stable_log_id,)).fetchone())

    def list_entries(self, *, station_id: int | None = None, date_from: str | None = None, date_to: str | None = None, limit: int | None = 1000) -> list[dict]:
        where: list[str] = []
        params: list = []
        if station_id is not None:
            where.append("station_id=?")
            params.append(int(station_id))
        if date_from:
            where.append("broadcast_at>=?")
            params.append(_sqlite_timestamp(date_from))
        if date_to:
            where.append("broadcast_at<?")
            params.append(_sqlite_timestamp(date_to))
        sql = "SELECT * FROM music_usage_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY broadcast_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def list_play_counts(
        self,
        *,
        station_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        where: list[str] = []
        params: list = []
        if station_id is not None:
            where.append("l.station_id=?")
            params.append(int(station_id))
        if date_from:
            where.append("l.broadcast_at>=?")
            params.append(_sqlite_timestamp(date_from))
        if date_to:
            where.append("l.broadcast_at<?")
            params.append(_sqlite_timestamp(date_to))
        sql = (
            "SELECT l.station_id, COALESCE(s.name, '') AS station_name, "
            "COALESCE(o.icecast_mount, '') AS mount, "
            "CASE WHEN COALESCE(o.icecast_enabled, 0)=1 "
            "AND TRIM(COALESCE(o.icecast_mount, ''))<>'' "
            "THEN 'configured_stream' ELSE 'historical_or_disabled' END AS mount_status, "
            "l.track_id, l.work_title, l.performer, l.composer, l.label, l.isrc, l.source_path, "
            "SUM(COALESCE(l.publication_count, 1)) AS completed_play_count, "
            "COUNT(*) AS event_count, "
            "ROUND(SUM(COALESCE(l.played_duration_seconds, 0)), 3) AS total_played_seconds, "
            "MIN(l.broadcast_at) AS first_broadcast_at, MAX(l.broadcast_at) AS last_broadcast_at "
            "FROM music_usage_log l LEFT JOIN stations s ON s.id=l.station_id "
            "LEFT JOIN station_outputs o ON o.station_id=l.station_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " GROUP BY l.station_id, s.name, o.icecast_mount, o.icecast_enabled, "
            "l.track_id, l.work_title, l.performer, "
            "l.composer, l.label, l.isrc, l.source_path "
            "ORDER BY l.station_id ASC, completed_play_count DESC, "
            "LOWER(l.work_title) ASC, LOWER(l.performer) ASC, l.track_id ASC"
        )
        return [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def verify_hash_chain(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM music_usage_log ORDER BY station_id ASC, id ASC"
        ).fetchall()
        expected_previous_by_station: dict[int, str] = {}
        final_hashes: dict[str, str] = {}
        hash_versions = {"v1_without_delivered_variants": 0, "v2": 0}
        for row in rows:
            entry = dict(row)
            station_id = int(entry.get("station_id") or 0)
            expected_previous = expected_previous_by_station.get(station_id, "")
            payload = {column: entry.get(column) for column in HASH_PAYLOAD_COLUMNS}
            canonical = json.dumps(
                {**payload, "previous_hash": expected_previous},
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if _text(entry.get("previous_hash"), 128) != expected_previous:
                return {
                    "valid": False,
                    "record_count": len(rows),
                    "station_id": station_id,
                    "log_id": _text(entry.get("log_id"), 160),
                    "reason": "previous_hash_mismatch",
                }
            stored_hash = _text(entry.get("entry_hash"), 128)
            hash_version = "v2"
            if stored_hash != expected_hash:
                # Version 1 predates delivery-variant reporting. Schema v20
                # added the column with a default without rewriting immutable
                # historical rows, so the original canonical payload remains
                # a valid and necessary verification path.
                legacy_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "delivered_variants_json"
                }
                legacy_canonical = json.dumps(
                    {**legacy_payload, "previous_hash": expected_previous},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                expected_hash = hashlib.sha256(
                    legacy_canonical.encode("utf-8")
                ).hexdigest()
                hash_version = "v1_without_delivered_variants"
            if stored_hash != expected_hash:
                return {
                    "valid": False,
                    "record_count": len(rows),
                    "station_id": station_id,
                    "log_id": _text(entry.get("log_id"), 160),
                    "reason": "entry_hash_mismatch",
                }
            expected_previous_by_station[station_id] = expected_hash
            final_hashes[str(station_id)] = expected_hash
            hash_versions[hash_version] += 1
        return {
            "valid": True,
            "record_count": len(rows),
            "station_final_hashes": final_hashes,
            "hash_versions": hash_versions,
        }

    def csv_text(self, entries: list[dict]) -> str:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(USAGE_COLUMNS)
        for entry in entries:
            writer.writerow([_csv_safe(entry.get(column, "")) for column in USAGE_COLUMNS])
        return output.getvalue()

    def play_count_csv_text(self, entries: list[dict]) -> str:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(PLAY_COUNT_COLUMNS)
        for entry in entries:
            writer.writerow(
                [_csv_safe(entry.get(column, "")) for column in PLAY_COUNT_COLUMNS]
            )
        return output.getvalue()

    @staticmethod
    def _atomic_write_text(destination: str | Path, content: str) -> dict:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="")
        temporary.replace(target)
        return {
            "path": str(target),
            "checksum": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def export_csv(self, *, destination: str | Path, station_id: int | None = None, date_from: str | None = None, date_to: str | None = None, entries: list[dict] | None = None) -> dict:
        if entries is None:
            entries = self.list_entries(
                station_id=station_id,
                date_from=date_from,
                date_to=date_to,
                limit=None,
            )
        result = self._atomic_write_text(destination, self.csv_text(entries))
        return {**result, "record_count": len(entries)}

    def export_official_current(self, *, destination: str | Path) -> dict:
        root = Path(destination)
        entries = self.list_entries(limit=None)
        integrity = self.verify_hash_chain()
        event_export = self.export_csv(
            destination=root / "RadioTEDU-music-usage-events-current.csv",
            entries=entries,
        )
        play_counts = self.list_play_counts()
        count_export = self._atomic_write_text(
            root / "RadioTEDU-music-play-counts-current.csv",
            self.play_count_csv_text(play_counts),
        )
        count_export["record_count"] = len(play_counts)
        generated_at = _utc_now().isoformat().replace("+00:00", "Z")
        manifest = {
            "generated_at_utc": generated_at,
            "integrity": integrity,
            "events": event_export,
            "play_counts": count_export,
            "definitions": {
                "completed_play_count": "Sum of immutable publication_count values after playout completion.",
                "event_count": "Number of immutable completed-play ledger rows.",
                "timestamps": "UTC",
            },
        }
        manifest_export = self._atomic_write_text(
            root / "RadioTEDU-music-usage-integrity-current.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {
            "generated_at_utc": generated_at,
            "integrity": integrity,
            "events": event_export,
            "play_counts": count_export,
            "manifest": manifest_export,
        }

    def close_month(self, *, year: int, month: int, closed_by: str = "system", export_path: str | Path | None = None, extra_entries: list[dict] | None = None) -> dict:
        start, end = _month_bounds(int(year), int(month))
        period_key = f"{int(year):04d}-{int(month):02d}"
        existing = self.conn.execute(
            "SELECT * FROM music_usage_month_closures WHERE period_key=?", (period_key,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        entries = self.list_entries(date_from=start, date_to=end, limit=None)
        if extra_entries:
            entries.extend(extra_entries)
            entries.sort(key=lambda entry: (str(entry.get("broadcast_at") or ""), str(entry.get("log_id") or "")))
        if export_path is None:
            export_path = get_data_dir() / "Exports" / "MusicUsage" / f"{period_key}.csv"
        export = self.export_csv(destination=export_path, entries=entries)
        digest = hashlib.sha256("\n".join(str(entry["entry_hash"]) for entry in entries).encode("utf-8")).hexdigest()
        first_hash = str(entries[0]["entry_hash"]) if entries else ""
        last_hash = str(entries[-1]["entry_hash"]) if entries else ""
        self.conn.execute(
            "INSERT INTO music_usage_month_closures "
            "(period_key, period_start, period_end, record_count, first_entry_hash, last_entry_hash, export_path, checksum, closed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (period_key, start, end, len(entries), first_hash, last_hash, str(export["path"]), digest, _text(closed_by, 160)),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM music_usage_month_closures WHERE period_key=?", (period_key,)).fetchone())

    def ensure_daily_exports(self, *, now: date | None = None) -> dict:
        today = now or _utc_now().date()
        previous = today - timedelta(days=1)
        destination = get_data_dir() / "Exports" / "MusicUsage" / f"{previous.isoformat()}.csv"
        daily = self.export_csv(destination=destination, date_from=previous.isoformat(), date_to=today.isoformat())
        closed = None
        if today.day == 1:
            month = previous.month
            closed = self.close_month(year=previous.year, month=month)
        return {"daily": daily, "monthly_close": closed}
