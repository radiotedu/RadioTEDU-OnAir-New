class ProgramQueueRepository:
    def __init__(self, conn):
        self.conn = conn

    def list_items(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT pq.id, pq.station_id, pq.track_id, pq.position, pq.created_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.file_path, '') AS file_path, COALESCE(t.duration, 0) AS duration "
            "FROM program_queue_items pq "
            "LEFT JOIN tracks t ON t.id = pq.track_id "
            "WHERE pq.station_id=? "
            "ORDER BY pq.position ASC, pq.id ASC",
            (int(station_id),),
        )
        return cur.fetchall()

    def next_pending(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT pq.id, pq.station_id, pq.track_id, pq.position, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.file_path, '') AS file_path, COALESCE(t.duration, 0) AS duration "
            "FROM program_queue_items pq "
            "LEFT JOIN tracks t ON t.id = pq.track_id "
            "WHERE pq.station_id=? "
            "ORDER BY pq.position ASC, pq.id ASC LIMIT 1",
            (int(station_id),),
        )
        return cur.fetchone()

    def pop_item(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM program_queue_items WHERE id=?", (int(item_id),))
        self.conn.commit()

    def _next_position(self, station_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM program_queue_items WHERE station_id=?",
            (int(station_id),),
        )
        return int(cur.fetchone()[0])

    def add_item(self, station_id: int, track_id: int) -> int:
        cur = self.conn.cursor()
        pos = self._next_position(station_id)
        cur.execute(
            "INSERT INTO program_queue_items (station_id, track_id, position) VALUES (?, ?, ?)",
            (int(station_id), int(track_id), pos),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _reindex(self, station_id: int) -> None:
        rows = list(self.list_items(station_id))
        cur = self.conn.cursor()
        for pos, row in enumerate(rows, start=1):
            cur.execute("UPDATE program_queue_items SET position=? WHERE id=?", (pos, int(row["id"])))
        self.conn.commit()

    def move_by_index(self, station_id: int, from_index: int, to_index: int) -> bool:
        rows = list(self.list_items(station_id))
        frm = int(from_index)
        to = int(to_index)
        if frm < 0 or frm >= len(rows):
            return False
        if to < 0:
            to = 0
        if to >= len(rows):
            to = len(rows) - 1
        row = rows.pop(frm)
        rows.insert(to, row)
        cur = self.conn.cursor()
        for pos, entry in enumerate(rows, start=1):
            cur.execute("UPDATE program_queue_items SET position=? WHERE id=?", (pos, int(entry["id"])))
        self.conn.commit()
        return True

    def delete_by_index(self, station_id: int, queue_index: int) -> bool:
        rows = list(self.list_items(station_id))
        idx = int(queue_index)
        if idx < 0 or idx >= len(rows):
            return False
        target = int(rows[idx]["id"])
        cur = self.conn.cursor()
        cur.execute("DELETE FROM program_queue_items WHERE id=?", (target,))
        self.conn.commit()
        self._reindex(station_id)
        return True

    def clear(self, station_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM program_queue_items WHERE station_id=?", (int(station_id),))
        self.conn.commit()

    def set_source(self, station_id: int, source: str) -> None:
        value = "host" if str(source or "").strip().lower() == "host" else "automation"
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, 'program_queue_source', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (int(station_id), value),
        )
        self.conn.commit()

    def get_source(self, station_id: int) -> str:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT value FROM station_settings WHERE station_id=? AND key='program_queue_source'",
            (int(station_id),),
        )
        row = cur.fetchone()
        if not row:
            return "automation"
        value = str(row["value"] or "").strip().lower()
        return "host" if value == "host" else "automation"
