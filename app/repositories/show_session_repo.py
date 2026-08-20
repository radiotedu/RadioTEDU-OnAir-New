_VALID_STATUSES = {
    "preparing", "going_live", "intro_playing", "live",
    "break_outro", "on_break", "break_intro",
    "outro_playing", "ended",
}


class ShowSessionRepository:
    def __init__(self, conn):
        self.conn = conn

    def create(self, show_id: int, station_id: int, user_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO show_sessions (show_id, station_id, user_id, status) "
            "VALUES (?, ?, ?, 'preparing')",
            (int(show_id), int(station_id), int(user_id)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, session_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM show_sessions WHERE id = ?", (int(session_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_active_for_station(self, station_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM show_sessions WHERE station_id = ? AND status != 'ended' "
            "ORDER BY id DESC LIMIT 1",
            (int(station_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def update_status(self, session_id: int, new_status: str) -> dict | None:
        if new_status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        cur = self.conn.cursor()
        # Set started_at only on first transition to 'live'
        if new_status == "live":
            cur.execute(
                "UPDATE show_sessions SET status = ?, "
                "started_at = CASE WHEN started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (new_status, int(session_id)),
            )
        else:
            cur.execute(
                "UPDATE show_sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, int(session_id)),
            )
        self.conn.commit()
        return self.get(session_id)

    def end_session(self, session_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE show_sessions SET status = 'ended', "
            "ended_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (int(session_id),),
        )
        self.conn.commit()
        return self.get(session_id)

    def list_for_show(self, show_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM show_sessions WHERE show_id = ? ORDER BY id DESC",
            (int(show_id),),
        )
        return [dict(r) for r in cur.fetchall()]

    def end_stale_sessions(self) -> int:
        """End all non-ended sessions except 'preparing'. Used at startup."""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE show_sessions SET status = 'ended', "
            "ended_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE status NOT IN ('ended', 'preparing')"
        )
        count = int(cur.rowcount or 0)
        self.conn.commit()
        return count
