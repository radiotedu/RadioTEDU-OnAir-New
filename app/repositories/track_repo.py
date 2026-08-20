class TrackRepository:
    def __init__(self, conn):
        self.conn = conn

    def upsert(
        self,
        title: str,
        artist: str,
        file_path: str,
        musicbrainz_recordingid: str,
        station_id: int = 1,
        track_type: str = "music",
        album: str = "",
        genre: str = "",
        language: str = "",
        duration: float = 0.0,
        bpm: float = 0.0,
        is_active: bool | None = None,
        exclude_from_autoplay: bool | None = None,
    ) -> int:
        cur = self.conn.cursor()
        existing_id = None
        if file_path:
            cur.execute("SELECT id FROM tracks WHERE file_path=? LIMIT 1", (file_path,))
            row = cur.fetchone()
            if row:
                existing_id = int(row["id"])
        if existing_id is None and musicbrainz_recordingid:
            cur.execute(
                "SELECT id FROM tracks WHERE musicbrainz_recordingid=? LIMIT 1",
                (musicbrainz_recordingid,),
            )
            row = cur.fetchone()
            if row:
                existing_id = int(row["id"])

        if existing_id is not None:
            update_parts = [
                "title=?",
                "artist=?",
                "file_path=?",
                "musicbrainz_recordingid=?",
                "station_id=?",
                "track_type=?",
                "album=?",
                "genre=?",
                "language=?",
                "duration=?",
                "bpm=?",
            ]
            params: list = [
                title,
                artist,
                file_path,
                musicbrainz_recordingid,
                int(station_id),
                str(track_type or "music"),
                str(album or ""),
                str(genre or ""),
                str(language or ""),
                float(duration or 0.0),
                float(bpm or 0.0),
            ]
            if is_active is not None:
                update_parts.append("is_active=?")
                params.append(1 if is_active else 0)
            if exclude_from_autoplay is not None:
                update_parts.append("exclude_from_autoplay=?")
                params.append(1 if exclude_from_autoplay else 0)
            cur.execute(
                f"UPDATE tracks SET {', '.join(update_parts)} WHERE id=?",
                (*params, existing_id),
            )
            self.conn.commit()
            return existing_id

        insert_columns = [
            "station_id",
            "title",
            "artist",
            "album",
            "genre",
            "language",
            "duration",
            "bpm",
            "track_type",
            "file_path",
            "musicbrainz_recordingid",
        ]
        insert_values: list = [
            int(station_id),
            title,
            artist,
            str(album or ""),
            str(genre or ""),
            str(language or ""),
            float(duration or 0.0),
            float(bpm or 0.0),
            str(track_type or "music"),
            file_path,
            musicbrainz_recordingid,
        ]
        if is_active is not None:
            insert_columns.append("is_active")
            insert_values.append(1 if is_active else 0)
        if exclude_from_autoplay is not None:
            insert_columns.append("exclude_from_autoplay")
            insert_values.append(1 if exclude_from_autoplay else 0)
        placeholders = ",".join("?" for _ in insert_columns)
        cur.execute(
            f"INSERT INTO tracks ({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(insert_values),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_generated_announcement(
        self,
        *,
        station_id: int,
        title: str,
        file_path: str,
        duration: float = 0.0,
        artist: str = "AI Host",
    ) -> int:
        return self.upsert(
            title=title,
            artist=artist,
            file_path=file_path,
            musicbrainz_recordingid="",
            station_id=station_id,
            track_type="announcement",
            duration=duration,
            is_active=False,
            exclude_from_autoplay=True,
        )

    def get(self, track_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, station_id, title, artist, album, genre, language, duration, bpm, track_type, "
            "file_path, musicbrainz_recordingid, is_active, exclude_from_autoplay, play_count, last_played_at "
            "FROM tracks WHERE id=?",
            (track_id,),
        )
        return cur.fetchone()

    def update(
        self,
        track_id: int,
        title: str,
        artist: str,
        album: str,
        genre: str,
        language: str,
        track_type: str,
        exclude_from_autoplay: bool,
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE tracks SET title=?, artist=?, album=?, genre=?, language=?, track_type=?, "
            "exclude_from_autoplay=? WHERE id=?",
            (
                str(title or ""),
                str(artist or ""),
                str(album or ""),
                str(genre or ""),
                str(language or ""),
                str(track_type or "music"),
                1 if exclude_from_autoplay else 0,
                int(track_id),
            ),
        )
        self.conn.commit()
        return int(cur.rowcount or 0) > 0

    def mark_played(self, track_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE tracks SET play_count=COALESCE(play_count, 0) + 1, last_played_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(track_id),),
        )
        self.conn.commit()

    def list(self, q: str = "", limit: int = 50):
        safe_limit = max(1, min(int(limit), 500))
        cur = self.conn.cursor()
        term = str(q or "").strip()
        if term:
            like = f"%{term}%"
            cur.execute(
                "SELECT id, station_id, title, artist, album, genre, language, duration, bpm, track_type, "
                "file_path, musicbrainz_recordingid, is_active, exclude_from_autoplay, play_count, last_played_at "
                "FROM tracks "
                "WHERE (title LIKE ? OR artist LIKE ? OR file_path LIKE ? OR musicbrainz_recordingid LIKE ?) AND is_active=1 "
                "ORDER BY id DESC LIMIT ?",
                (like, like, like, like, safe_limit),
            )
        else:
            cur.execute(
                "SELECT id, station_id, title, artist, album, genre, language, duration, bpm, track_type, "
                "file_path, musicbrainz_recordingid, is_active, exclude_from_autoplay, play_count, last_played_at "
                "FROM tracks WHERE is_active=1 ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            )
        return cur.fetchall()

    def deactivate(self, track_id: int) -> dict:
        tid = int(track_id)
        cur = self.conn.cursor()
        cur.execute("SELECT id, is_active FROM tracks WHERE id=? LIMIT 1", (tid,))
        row = cur.fetchone()
        if row is None:
            return {"found": False}
        if int(row["is_active"] or 0) == 0:
            return {
                "found": True,
                "deleted": False,
                "already_inactive": True,
                "queue_failed": 0,
                "schedule_failed": 0,
                "ad_failed": 0,
                "playlist_items_removed": 0,
                "program_items_removed": 0,
                "playout_reset": 0,
                "affected_stations": [],
                "runtime_stop_stations": [],
            }

        affected_stations: set[int] = set()
        runtime_stop_stations: set[int] = set()
        for table in ("queue_items", "schedule_items", "ad_break_items"):
            cur.execute(
                f"SELECT DISTINCT station_id FROM {table} WHERE track_id=?",
                (tid,),
            )
            for station_row in cur.fetchall():
                affected_stations.add(int(station_row["station_id"]))
            cur.execute(
                f"SELECT DISTINCT station_id FROM {table} WHERE track_id=? AND status='playing'",
                (tid,),
            )
            for station_row in cur.fetchall():
                runtime_stop_stations.add(int(station_row["station_id"]))

        cur.execute("UPDATE tracks SET is_active=0 WHERE id=?", (tid,))
        cur.execute(
            "UPDATE queue_items SET status='failed', finished_at=CURRENT_TIMESTAMP "
            "WHERE track_id=? AND status IN ('pending','playing')",
            (tid,),
        )
        queue_failed = int(cur.rowcount or 0)

        cur.execute(
            "UPDATE schedule_items SET status='failed' "
            "WHERE track_id=? AND status IN ('pending','playing')",
            (tid,),
        )
        schedule_failed = int(cur.rowcount or 0)

        cur.execute(
            "UPDATE ad_break_items SET status='failed' "
            "WHERE track_id=? AND status IN ('pending','playing')",
            (tid,),
        )
        ad_failed = int(cur.rowcount or 0)

        cur.execute("DELETE FROM playlist_items WHERE track_id=?", (tid,))
        playlist_items_removed = int(cur.rowcount or 0)

        cur.execute("DELETE FROM program_queue_items WHERE track_id=?", (tid,))
        program_items_removed = int(cur.rowcount or 0)

        cur.execute(
            "SELECT station_id FROM playout_state "
            "WHERE current_source='library_fallback' AND current_item_id=?",
            (tid,),
        )
        for station_row in cur.fetchall():
            runtime_stop_stations.add(int(station_row["station_id"]))
        cur.execute(
            "UPDATE playout_state SET current_source='none', current_item_id=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE current_source='library_fallback' AND current_item_id=?",
            (tid,),
        )
        playout_reset = int(cur.rowcount or 0)

        self.conn.commit()
        return {
            "found": True,
            "deleted": True,
            "already_inactive": False,
            "queue_failed": queue_failed,
            "schedule_failed": schedule_failed,
            "ad_failed": ad_failed,
            "playlist_items_removed": playlist_items_removed,
            "program_items_removed": program_items_removed,
            "playout_reset": playout_reset,
            "affected_stations": sorted(affected_stations),
            "runtime_stop_stations": sorted(runtime_stop_stations),
        }
