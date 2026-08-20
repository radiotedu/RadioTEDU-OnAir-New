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
            "WHERE station_id=? AND status='pending' AND datetime(due_at) <= CURRENT_TIMESTAMP "
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
