class PlaylistRepository:
    def __init__(self, conn):
        self.conn = conn

    def create(
        self,
        station_id: int,
        name: str,
        description: str = "",
        playlist_type: str = "manual",
    ) -> int:
        kind = str(playlist_type or "manual").strip().lower()
        if kind not in {"manual", "auto"}:
            kind = "manual"
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO playlists (station_id, name, description, playlist_type) VALUES (?, ?, ?, ?)",
            (
                int(station_id),
                str(name or "").strip(),
                str(description or "").strip(),
                kind,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_for_station(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT p.id, p.station_id, p.name, p.description, p.playlist_type, p.created_at, "
            "COALESCE(COUNT(pi.id), 0) AS item_count "
            "FROM playlists p "
            "LEFT JOIN playlist_items pi ON pi.playlist_id=p.id "
            "WHERE p.station_id=? "
            "GROUP BY p.id, p.station_id, p.name, p.description, p.playlist_type, p.created_at "
            "ORDER BY p.id DESC",
            (int(station_id),),
        )
        return cur.fetchall()

    def get(self, playlist_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT p.id, p.station_id, p.name, p.description, p.playlist_type, p.created_at, "
            "COALESCE(COUNT(pi.id), 0) AS item_count "
            "FROM playlists p "
            "LEFT JOIN playlist_items pi ON pi.playlist_id=p.id "
            "WHERE p.id=? "
            "GROUP BY p.id, p.station_id, p.name, p.description, p.playlist_type, p.created_at",
            (int(playlist_id),),
        )
        return cur.fetchone()

    def delete(self, playlist_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM playlist_items WHERE playlist_id=?", (int(playlist_id),))
        cur.execute("DELETE FROM playlists WHERE id=?", (int(playlist_id),))
        self.conn.commit()

    def next_position(self, playlist_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_items WHERE playlist_id=?",
            (int(playlist_id),),
        )
        return int(cur.fetchone()[0])

    def add_item(self, playlist_id: int, track_id: int) -> int:
        cur = self.conn.cursor()
        pos = self.next_position(playlist_id)
        cur.execute(
            "INSERT INTO playlist_items (playlist_id, track_id, position) VALUES (?, ?, ?)",
            (int(playlist_id), int(track_id), pos),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_items(self, playlist_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT pi.id AS item_id, pi.playlist_id, pi.track_id, pi.position, pi.created_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, COALESCE(t.duration, 0) AS duration "
            "FROM playlist_items pi "
            "LEFT JOIN tracks t ON t.id=pi.track_id "
            "WHERE pi.playlist_id=? "
            "ORDER BY pi.position ASC, pi.id ASC",
            (int(playlist_id),),
        )
        return cur.fetchall()

    def delete_item(self, playlist_id: int, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM playlist_items WHERE playlist_id=? AND id=?",
            (int(playlist_id), int(item_id)),
        )
        self.conn.commit()

    def reorder(self, playlist_id: int, item_ids: list[int]) -> None:
        cur = self.conn.cursor()
        for pos, item_id in enumerate(item_ids, start=1):
            cur.execute(
                "UPDATE playlist_items SET position=? WHERE playlist_id=? AND id=?",
                (int(pos), int(playlist_id), int(item_id)),
            )
        self.conn.commit()

    def bulk_add(self, playlist_id: int, track_ids: list[int]) -> list[int]:
        created = []
        for track_id in track_ids:
            created.append(self.add_item(playlist_id=playlist_id, track_id=int(track_id)))
        return created
