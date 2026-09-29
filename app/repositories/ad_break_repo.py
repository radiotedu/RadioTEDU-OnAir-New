_MAX_RETRY_AFTER_SECONDS = 30 * 24 * 60 * 60
_MAX_RETRY_COUNT = 10_000
_MAX_LAST_ERROR_CHARS = 512


class AdBreakRepository:
    def __init__(self, conn):
        self.conn = conn

    def enqueue(
        self,
        station_id: int,
        track_id: int,
        due_at: str,
        priority: int = 0,
        dedupe_key: str | None = None,
    ) -> int:
        cur = self.conn.cursor()
        if dedupe_key:
            cur.execute(
                "SELECT id FROM ad_break_items WHERE station_id=? AND dedupe_key=? "
                "AND status IN ('pending','playing','done') ORDER BY id LIMIT 1",
                (int(station_id), str(dedupe_key)),
            )
            existing = cur.fetchone()
            if existing is not None:
                return int(existing["id"])
        cur.execute(
            "INSERT INTO ad_break_items "
            "(station_id, track_id, due_at, status, priority, dedupe_key) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (station_id, track_id, due_at, int(priority), dedupe_key),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def next_due(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM ad_break_items "
            "WHERE station_id=? AND status='pending' "
            "AND datetime(due_at) <= CURRENT_TIMESTAMP "
            "AND (retry_after IS NULL OR datetime(retry_after) <= CURRENT_TIMESTAMP) "
            "ORDER BY priority DESC, datetime(due_at) ASC, id ASC "
            "LIMIT 1",
            (station_id,),
        )
        return cur.fetchone()

    def current_playing(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.*, COALESCE(t.title, '') AS title, "
            "COALESCE(t.artist, '') AS artist, COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'ad') AS track_type "
            "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
            "WHERE a.station_id=? AND a.status='playing' "
            "ORDER BY a.started_at ASC, a.id ASC LIMIT 1",
            (int(station_id),),
        )
        return cur.fetchone()

    def list_active(self, station_id: int, limit: int = 100):
        safe_limit = max(1, min(int(limit), 500))
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.*, COALESCE(t.title, '') AS title, "
            "COALESCE(t.artist, '') AS artist, COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'ad') AS track_type "
            "FROM ad_break_items a LEFT JOIN tracks t ON t.id=a.track_id "
            "WHERE a.station_id=? AND a.status IN ('pending','playing') "
            "ORDER BY CASE a.status WHEN 'playing' THEN 0 ELSE 1 END, "
            "datetime(a.due_at), a.priority DESC, a.id LIMIT ?",
            (int(station_id), safe_limit),
        )
        return cur.fetchall()

    def list_recent(self, station_id: int, limit: int = 20):
        safe_limit = max(1, min(int(limit), 200))
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.id, a.station_id, a.track_id, a.due_at, a.status, a.priority, "
            "a.started_at, a.finished_at, a.dedupe_key, "
            "a.retry_after, a.retry_count, a.last_error, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist "
            "FROM ad_break_items a "
            "LEFT JOIN tracks t ON t.id = a.track_id "
            "WHERE a.station_id=? "
            "ORDER BY a.id DESC "
            "LIMIT ?",
            (station_id, safe_limit),
        )
        return cur.fetchall()

    def mark_playing(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_break_items SET status='playing', "
            "started_at=CASE WHEN status='playing' AND started_at IS NOT NULL "
            "THEN started_at ELSE CURRENT_TIMESTAMP END WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def mark_done(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_break_items SET status='done', finished_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def mark_failed(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_break_items SET status='failed', finished_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def defer(
        self, item_id: int, retry_after_seconds: int | float, error: str = ""
    ) -> int:
        """Release a failed playing ad while preserving its scheduled due time."""
        try:
            seconds = int(float(retry_after_seconds))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("retry_after_seconds must be a finite number") from exc
        seconds = max(0, min(seconds, _MAX_RETRY_AFTER_SECONDS))
        safe_error = str(error or "")[:_MAX_LAST_ERROR_CHARS]
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_break_items SET status='pending', started_at=NULL, "
            "finished_at=NULL, retry_after=datetime(CURRENT_TIMESTAMP, ?), "
            "retry_count=MIN(MAX(COALESCE(retry_count, 0), 0) + 1, ?), last_error=? "
            "WHERE id=? AND status='playing'",
            (
                f"+{seconds} seconds",
                _MAX_RETRY_COUNT,
                safe_error,
                int(item_id),
            ),
        )
        self.conn.commit()
        return max(0, int(cur.rowcount or 0))
