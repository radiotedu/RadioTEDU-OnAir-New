import time
from pathlib import Path

from app.audio import audio_processing
from app.media_paths import resolve_runtime_media_path


class PlayoutStateService:
    def __init__(self, conn):
        self.conn = conn

    def get_current(self, station_id: int) -> dict:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT current_source, current_item_id FROM playout_state WHERE station_id=?",
            (int(station_id),),
        )
        row = cur.fetchone()
        if not row:
            return {"source": "none", "item_id": None}
        return {
            "source": str(row["current_source"] or "none"),
            "item_id": row["current_item_id"],
        }

    def set_current(
        self,
        station_id: int,
        source: str,
        item_id: int | None,
        *,
        reason: str = "",
    ):
        previous = self.get_current(int(station_id))
        next_source = str(source or "none")
        next_item_id = int(item_id) if item_id is not None else None
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO playout_state (station_id, current_source, current_item_id) VALUES (?, ?, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET current_source=excluded.current_source, current_item_id=excluded.current_item_id, updated_at=CURRENT_TIMESTAMP",
            (station_id, next_source, next_item_id),
        )
        if (
            str(previous["source"]) != next_source
            or previous["item_id"] != next_item_id
        ):
            cur.execute(
                "INSERT INTO playout_transitions "
                "(station_id, from_source, from_item_id, to_source, to_item_id, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(station_id),
                    str(previous["source"] or "none"),
                    previous["item_id"],
                    next_source,
                    next_item_id,
                    str(reason or ""),
                ),
            )
        self.conn.commit()

    def list_recent(self, station_id: int, limit: int = 100) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM playout_transitions WHERE station_id=? "
            "ORDER BY id DESC LIMIT ?",
            (int(station_id), max(1, min(1000, int(limit)))),
        )
        return [dict(row) for row in cur.fetchall()]

    def reconcile_startup(self, station_id: int) -> None:
        cur = self.conn.cursor()
        previous = self.get_current(int(station_id))
        cur.execute(
            "UPDATE queue_items SET status='pending', started_at=NULL, finished_at=NULL "
            "WHERE station_id=? AND status='playing'",
            (station_id,),
        )
        cur.execute(
            "INSERT INTO playout_state (station_id, current_source, current_item_id) VALUES (?, 'none', NULL) "
            "ON CONFLICT(station_id) DO UPDATE SET current_source='none', current_item_id=NULL, updated_at=CURRENT_TIMESTAMP",
            (station_id,),
        )
        if previous["source"] != "none" or previous["item_id"] is not None:
            cur.execute(
                "INSERT INTO playout_transitions "
                "(station_id, from_source, from_item_id, to_source, to_item_id, reason) "
                "VALUES (?, ?, ?, 'none', NULL, 'startup_reconcile')",
                (
                    int(station_id),
                    str(previous["source"] or "none"),
                    previous["item_id"],
                ),
            )
        self.conn.commit()


def _backfill_missing_track_durations(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, COALESCE(file_path, '') AS file_path "
        "FROM tracks "
        "WHERE is_active=1 AND COALESCE(duration, 0) <= 0 AND TRIM(COALESCE(file_path, '')) <> '' "
        "ORDER BY CASE WHEN EXISTS ("
        "SELECT 1 FROM queue_items q WHERE q.track_id=tracks.id AND q.status IN ('pending', 'playing')"
        ") THEN 0 ELSE 1 END, id LIMIT 64"
    )
    updated = 0
    deadline = time.monotonic() + 10.0
    for row in cur.fetchall():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        raw_path = str(row["file_path"] or "").strip()
        resolved_path = resolve_runtime_media_path(raw_path)
        if not resolved_path or "://" in resolved_path:
            continue
        candidate = Path(resolved_path)
        if not candidate.is_file():
            continue
        duration = float(
            audio_processing.probe_duration(
                str(candidate),
                timeout_seconds=min(3.0, remaining),
            )
        )
        if duration <= 0:
            continue
        cur.execute(
            "UPDATE tracks SET duration=? WHERE id=?",
            (duration, int(row["id"])),
        )
        updated += int(cur.rowcount or 0)
    return updated


def reconcile_all_startup(conn) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT station_id, current_source, current_item_id FROM playout_state "
        "WHERE COALESCE(current_source, 'none') <> 'none' OR current_item_id IS NOT NULL"
    )
    stale_playout = list(cur.fetchall())
    cur.execute(
        "UPDATE queue_items SET status='pending', started_at=NULL, finished_at=NULL "
        "WHERE status='playing'"
    )
    queue_requeued = int(cur.rowcount or 0)

    cur.execute("UPDATE ad_break_items SET status='pending' WHERE status='playing'")
    ad_requeued = int(cur.rowcount or 0)

    cur.execute("UPDATE schedule_items SET status='pending' WHERE status='playing'")
    schedule_requeued = int(cur.rowcount or 0)

    cur.execute(
        "UPDATE playout_state SET current_source='none', current_item_id=NULL, updated_at=CURRENT_TIMESTAMP "
        "WHERE COALESCE(current_source, 'none') <> 'none' OR current_item_id IS NOT NULL"
    )
    playout_reset = int(cur.rowcount or 0)
    for row in stale_playout:
        cur.execute(
            "INSERT INTO playout_transitions "
            "(station_id, from_source, from_item_id, to_source, to_item_id, reason) "
            "VALUES (?, ?, ?, 'none', NULL, 'startup_reconcile')",
            (
                int(row["station_id"]),
                str(row["current_source"] or "none"),
                row["current_item_id"],
            ),
        )

    cur.execute(
        "UPDATE show_sessions SET status='ended', ended_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
        "WHERE status NOT IN ('ended', 'preparing')"
    )
    show_sessions_ended = int(cur.rowcount or 0)
    if show_sessions_ended > 0:
        cur.execute(
            "UPDATE station_settings SET value='automation', updated_at=CURRENT_TIMESTAMP "
            "WHERE key='program_queue_source' AND value='host'"
        )

    track_durations_backfilled = _backfill_missing_track_durations(conn)

    conn.commit()
    return {
        "queue_requeued": queue_requeued,
        "ad_requeued": ad_requeued,
        "schedule_requeued": schedule_requeued,
        "playout_reset": playout_reset,
        "show_sessions_ended": show_sessions_ended,
        "track_durations_backfilled": track_durations_backfilled,
    }
