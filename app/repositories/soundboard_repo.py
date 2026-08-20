class SoundboardRepository:
    def __init__(self, conn):
        self.conn = conn

    def get(self, item_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM soundboard_items WHERE id = ?", (int(item_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_by_station(self, station_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM soundboard_items WHERE station_id = ? ORDER BY sort_order, id",
            (int(station_id),),
        )
        return [dict(r) for r in cur.fetchall()]

    def create(self, station_id: int, name: str, file_path: str, **kwargs) -> int:
        fields = {
            "station_id": int(station_id),
            "name": str(name),
            "file_path": str(file_path),
        }
        for key in ("color", "hotkey", "category", "duration_s", "gain_db", "sort_order", "uploaded"):
            if key in kwargs and kwargs[key] is not None:
                fields[key] = kwargs[key]
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.cursor()
        cur.execute(
            f"INSERT INTO soundboard_items ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update(self, item_id: int, **fields) -> dict | None:
        allowed = {"name", "file_path", "color", "hotkey", "category", "duration_s", "gain_db", "sort_order", "uploaded"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get(item_id)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [int(item_id)]
        cur = self.conn.cursor()
        cur.execute(f"UPDATE soundboard_items SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get(item_id)

    def delete(self, item_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM soundboard_items WHERE id = ?", (int(item_id),))
        self.conn.commit()
        return cur.rowcount > 0
