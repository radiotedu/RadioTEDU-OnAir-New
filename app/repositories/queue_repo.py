import hashlib


class QueueRepository:
    def __init__(self, conn):
        self.conn = conn

    def find_by_dedupe_key(
        self,
        station_id: int,
        dedupe_key: str | None,
        *,
        statuses: tuple[str, ...] = ("pending", "playing"),
    ):
        if not dedupe_key:
            return None
        safe_statuses = tuple(str(status or "").strip().lower() for status in statuses if str(status or "").strip())
        if not safe_statuses:
            return None
        placeholders = ",".join("?" for _ in safe_statuses)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, station_id, track_id, position, status, dedupe_key "
            "FROM queue_items "
            f"WHERE station_id=? AND dedupe_key=? AND status IN ({placeholders}) "
            "ORDER BY id ASC LIMIT 1",
            (int(station_id), str(dedupe_key), *safe_statuses),
        )
        return cur.fetchone()

    def change_sequence(self, station_id: int) -> int:
        row = self.conn.execute(
            "SELECT value FROM station_settings WHERE station_id=? AND key='_queue_change_sequence'",
            (int(station_id),),
        ).fetchone()
        try:
            return max(0, int(str(row["value"] if row else "0")))
        except (TypeError, ValueError):
            return 0

    def _bump_change_sequence_in_transaction(self, station_id: int) -> int:
        self.conn.execute(
            "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, '_queue_change_sequence', '1', CURRENT_TIMESTAMP) "
            "ON CONFLICT(station_id, key) DO UPDATE SET value=CAST(COALESCE(station_settings.value, '0') AS INTEGER)+1, updated_at=CURRENT_TIMESTAMP",
            (int(station_id),),
        )
        return self.change_sequence(station_id)

    def enqueue_or_get_existing(
        self,
        station_id: int,
        track_id: int,
        dedupe_key: str | None = None,
        *,
        manage_transaction: bool = True,
    ) -> tuple[int, bool]:
        owns_transaction = bool(manage_transaction and not self.conn.in_transaction)
        if owns_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.cursor()
            row = self.find_by_dedupe_key(
                station_id,
                dedupe_key,
                statuses=("pending", "playing"),
            )
            if row is not None:
                if owns_transaction:
                    self.conn.commit()
                return int(row["id"]), False

            cur.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM queue_items WHERE station_id=?",
                (station_id,),
            )
            pos = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) VALUES (?, ?, ?, 'pending', ?)",
                (station_id, track_id, pos, dedupe_key),
            )
            self._bump_change_sequence_in_transaction(station_id)
            if owns_transaction:
                self.conn.commit()
            return int(cur.lastrowid), True
        except Exception:
            if owns_transaction:
                self.conn.rollback()
            raise

    def enqueue(self, station_id: int, track_id: int, dedupe_key: str | None = None) -> int:
        item_id, _created = self.enqueue_or_get_existing(
            station_id=station_id,
            track_id=track_id,
            dedupe_key=dedupe_key,
        )
        return item_id

    def insert_before_item_or_get_existing(
        self,
        *,
        station_id: int,
        before_item_id: int,
        track_id: int,
        dedupe_key: str | None = None,
        dedupe_statuses: tuple[str, ...] = ("pending", "playing"),
    ) -> tuple[int, bool]:
        existing = self.find_by_dedupe_key(
            station_id,
            dedupe_key,
            statuses=dedupe_statuses,
        )
        if existing is not None:
            return int(existing["id"]), False

        cur = self.conn.cursor()
        cur.execute(
            "SELECT position FROM queue_items "
            "WHERE station_id=? AND id=? AND status IN ('pending','playing') "
            "LIMIT 1",
            (int(station_id), int(before_item_id)),
        )
        row = cur.fetchone()
        if row is None:
            return self.enqueue_or_get_existing(
                station_id=int(station_id),
                track_id=int(track_id),
                dedupe_key=dedupe_key,
            )

        insert_pos = int(row["position"] or 1)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE queue_items SET position=position+1 "
                "WHERE station_id=? AND status IN ('pending','playing') AND position>=?",
                (int(station_id), insert_pos),
            )
            cur.execute(
                "INSERT INTO queue_items (station_id, track_id, position, status, dedupe_key) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (int(station_id), int(track_id), insert_pos, dedupe_key),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(cur.lastrowid), True

    def next_pending(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.*, COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.duration, 0.0) AS duration, COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='pending' ORDER BY q.position ASC LIMIT 1",
            (station_id,),
        )
        return cur.fetchone()

    def mark_playing(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE queue_items SET status='playing', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def mark_done(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE queue_items SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def mark_failed(self, item_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE queue_items SET status='failed', finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (item_id,),
        )
        self.conn.commit()

    def list_recent(self, station_id: int, limit: int = 20):
        safe_limit = max(1, min(int(limit), 200))
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id, q.station_id, q.track_id, q.position, q.status, q.enqueued_at, q.started_at, q.finished_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? "
            "ORDER BY q.id DESC "
            "LIMIT ?",
            (station_id, safe_limit),
        )
        return cur.fetchall()

    def list_done_recent(self, station_id: int, limit: int = 3):
        safe_limit = max(1, min(int(limit), 20))
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id, q.station_id, q.track_id, q.position, q.status, "
            "q.enqueued_at, q.started_at, q.finished_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='done' "
            "ORDER BY q.id DESC LIMIT ?",
            (int(station_id), safe_limit),
        )
        rows = cur.fetchall()
        return list(reversed(rows))  # oldest first so table reads top-to-bottom chronologically

    def current_playing(self, station_id: int):
        """Return the currently playing queue item (if any) with track duration."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id, q.station_id, q.track_id, q.position, q.status, "
            "q.started_at, COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status='playing' "
            "ORDER BY q.id ASC LIMIT 1",
            (int(station_id),),
        )
        return cur.fetchone()

    def list_active_ordered(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT q.id, q.station_id, q.track_id, q.position, q.status, q.enqueued_at, q.started_at, q.finished_at, "
            "COALESCE(t.title, '') AS title, COALESCE(t.artist, '') AS artist, "
            "COALESCE(t.duration, 0.0) AS duration, "
            "COALESCE(t.track_type, 'music') AS track_type "
            "FROM queue_items q "
            "LEFT JOIN tracks t ON t.id = q.track_id "
            "WHERE q.station_id=? AND q.status IN ('pending','playing') "
            "ORDER BY q.position ASC, q.id ASC",
            (int(station_id),),
        )
        return cur.fetchall()

    def _reindex_active(self, station_id: int) -> None:
        cur = self.conn.cursor()
        rows = self.list_active_ordered(station_id)
        for idx, row in enumerate(rows, start=1):
            cur.execute("UPDATE queue_items SET position=? WHERE id=?", (idx, int(row["id"])))
        self.conn.commit()

    @staticmethod
    def active_revision(rows) -> str:
        """Stable optimistic-concurrency token for the actionable queue state."""
        material = "|".join(
            f"{int(row['id'])}:{int(row['position'])}:{str(row['status'])}"
            for row in rows
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _reindex_active_in_transaction(self, station_id: int) -> None:
        cur = self.conn.cursor()
        for idx, row in enumerate(self.list_active_ordered(station_id), start=1):
            cur.execute("UPDATE queue_items SET position=? WHERE id=?", (idx, int(row["id"])))

    def delete_pending_item(
        self,
        *,
        station_id: int,
        item_id: int,
        expected_revision: str,
    ) -> str:
        """Delete one pending item only if the operator snapshot is still current."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = list(self.list_active_ordered(station_id))
            if self.active_revision(rows) != str(expected_revision):
                self.conn.rollback()
                return "stale"
            target = next((row for row in rows if int(row["id"]) == int(item_id)), None)
            if target is None:
                self.conn.rollback()
                return "missing"
            if str(target["status"]) != "pending":
                self.conn.rollback()
                return "playing"
            cur = self.conn.cursor()
            cur.execute(
                "DELETE FROM queue_items WHERE id=? AND station_id=? AND status='pending'",
                (int(item_id), int(station_id)),
            )
            if cur.rowcount != 1:
                self.conn.rollback()
                return "stale"
            self._reindex_active_in_transaction(station_id)
            self._bump_change_sequence_in_transaction(station_id)
            self.conn.commit()
            return "ok"
        except Exception:
            self.conn.rollback()
            raise

    def move_pending_item(
        self,
        *,
        station_id: int,
        item_id: int,
        to_index: int,
        expected_revision: str,
    ) -> str:
        """Move one pending item inside the pending section of a known snapshot."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = list(self.list_active_ordered(station_id))
            if self.active_revision(rows) != str(expected_revision):
                self.conn.rollback()
                return "stale"
            from_index = next(
                (index for index, row in enumerate(rows) if int(row["id"]) == int(item_id)),
                None,
            )
            if from_index is None:
                self.conn.rollback()
                return "missing"
            if str(rows[from_index]["status"]) != "pending":
                self.conn.rollback()
                return "playing"
            pending_indexes = [
                index for index, row in enumerate(rows) if str(row["status"]) == "pending"
            ]
            if int(to_index) not in pending_indexes:
                self.conn.rollback()
                return "invalid_destination"
            target_index = int(to_index)
            if from_index != target_index:
                item = rows.pop(from_index)
                rows.insert(target_index, item)
                cur = self.conn.cursor()
                for position, row in enumerate(rows, start=1):
                    cur.execute(
                        "UPDATE queue_items SET position=? WHERE id=?",
                        (position, int(row["id"])),
                    )
                self._bump_change_sequence_in_transaction(station_id)
            self.conn.commit()
            return "ok"
        except Exception:
            self.conn.rollback()
            raise

    def skip_playing_item(
        self, *, station_id: int, item_id: int, expected_revision: str
    ) -> str:
        """Atomically finish a playing row for callers without runtime control."""
        outcome = self.begin_skip_playing_item(
            station_id=station_id,
            item_id=item_id,
            expected_revision=expected_revision,
        )
        if outcome != "ok":
            return outcome
        try:
            return self.commit_skip_playing_item(
                station_id=station_id,
                item_id=item_id,
            )
        except Exception:
            self.rollback_skip_playing_item()
            raise

    def begin_skip_playing_item(
        self, *, station_id: int, item_id: int, expected_revision: str
    ) -> str:
        """Acquire the queue write lock and validate one current row.

        The successful return deliberately leaves ``BEGIN IMMEDIATE`` open.  The
        operator skip path holds that lock while stopping the old runtime, so no
        other operator can invalidate the checked revision in between.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = list(self.list_active_ordered(station_id))
            if self.active_revision(rows) != str(expected_revision):
                self.conn.rollback()
                return "stale"
            target = next((row for row in rows if int(row["id"]) == int(item_id)), None)
            if target is None:
                self.conn.rollback()
                return "missing"
            if str(target["status"]) != "playing":
                self.conn.rollback()
                return "not_playing"
            return "ok"
        except Exception:
            self.conn.rollback()
            raise

    def commit_skip_playing_item(self, *, station_id: int, item_id: int) -> str:
        """Persist a prepared skip and release its transaction."""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE queue_items SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=? AND station_id=? AND status='playing'",
                (int(item_id), int(station_id)),
            )
            if cur.rowcount != 1:
                self.conn.rollback()
                return "stale"
            self._bump_change_sequence_in_transaction(station_id)
            self.conn.commit()
            return "ok"
        except Exception:
            self.conn.rollback()
            raise

    def rollback_skip_playing_item(self) -> None:
        """Best-effort release for an abandoned prepared operator skip."""
        try:
            self.conn.rollback()
        except Exception:
            pass

    def delete_by_queue_index(self, station_id: int, queue_index: int) -> bool:
        rows = self.list_active_ordered(station_id)
        idx = int(queue_index)
        if idx < 0 or idx >= len(rows):
            return False
        target_id = int(rows[idx]["id"])
        cur = self.conn.cursor()
        cur.execute("DELETE FROM queue_items WHERE id=?", (target_id,))
        self.conn.commit()
        self._reindex_active(station_id)
        return True

    def move_by_queue_index(self, station_id: int, from_index: int, to_index: int) -> bool:
        rows = list(self.list_active_ordered(station_id))
        frm = int(from_index)
        to = int(to_index)
        if frm < 0 or frm >= len(rows):
            return False
        if to < 0:
            to = 0
        if to >= len(rows):
            to = len(rows) - 1
        if frm == to:
            return True
        row = rows.pop(frm)
        rows.insert(to, row)
        cur = self.conn.cursor()
        for pos, entry in enumerate(rows, start=1):
            cur.execute("UPDATE queue_items SET position=? WHERE id=?", (pos, int(entry["id"])))
        self.conn.commit()
        return True
