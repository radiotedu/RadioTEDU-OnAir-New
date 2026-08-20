class StudioRepository:
    _UPDATABLE_COLUMNS = {
        "name",
        "description",
        "sort_order",
        "is_active",
        "is_on_air",
        "current_user_id",
    }

    def __init__(self, conn):
        self.conn = conn

    def get(self, studio_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM studios WHERE id=?", (int(studio_id),))
        return cur.fetchone()

    def list_by_station(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, station_id, name, description, sort_order, is_active, "
            "is_on_air, current_user_id, created_at, updated_at "
            "FROM studios WHERE station_id=? ORDER BY sort_order ASC, id ASC",
            (int(station_id),),
        )
        return cur.fetchall()

    def create(
        self,
        station_id: int,
        name: str,
        description: str = "",
        sort_order: int = 1,
        is_active: bool = True,
        is_on_air: bool = False,
        current_user_id: int | None = None,
        commit: bool = True,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO studios (station_id, name, description, sort_order, is_active, is_on_air, current_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(station_id),
                str(name or "").strip(),
                str(description or "").strip(),
                int(sort_order or 1),
                1 if is_active else 0,
                1 if is_on_air else 0,
                int(current_user_id) if current_user_id is not None else None,
            ),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def ensure_default_for_station(self, station_id: int, commit: bool = True) -> None:
        if self.list_by_station(station_id):
            return
        self.create(
            station_id=station_id,
            name="Studio A",
            description="",
            sort_order=1,
            is_active=True,
            is_on_air=True,
            current_user_id=None,
            commit=commit,
        )

    def update(self, studio_id: int, **fields) -> bool:
        updates = {}
        for key, value in dict(fields).items():
            column = str(key)
            if column not in self._UPDATABLE_COLUMNS:
                continue
            if column != "current_user_id" and value is None:
                continue
            updates[column] = int(value) if column == "current_user_id" and value is not None else value
        if not updates:
            return False
        clauses = [f"{key}=?" for key in updates]
        values = list(updates.values())
        values.append(int(studio_id))
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE studios SET {', '.join(clauses)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            tuple(values),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, studio_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM studios WHERE id=?", (int(studio_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def set_on_air(self, studio_id: int, is_on_air: bool) -> bool:
        return self.update(studio_id, is_on_air=1 if is_on_air else 0)

    def set_current_user(self, studio_id: int, user_id: int | None) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE studios SET current_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(user_id) if user_id is not None else None, int(studio_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def upsert_session(
        self,
        studio_id: int,
        user_id: int,
        session_role: str,
        status: str,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO studio_sessions (studio_id, user_id, session_role, status, joined_at, left_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP) "
            "ON CONFLICT(studio_id, user_id) DO UPDATE SET "
            "session_role=excluded.session_role, "
            "status=excluded.status, "
            "joined_at=CURRENT_TIMESTAMP, "
            "left_at=NULL, "
            "last_seen_at=CURRENT_TIMESTAMP",
            (
                int(studio_id),
                int(user_id),
                str(session_role or "dj"),
                str(status or "active"),
            ),
        )
        self.conn.commit()
        cur.execute(
            "SELECT id FROM studio_sessions WHERE studio_id=? AND user_id=?",
            (int(studio_id), int(user_id)),
        )
        row = cur.fetchone()
        return int(row["id"]) if row is not None else 0

    def list_sessions(self, studio_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM studio_sessions WHERE studio_id=? ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'standby' THEN 1 ELSE 2 END, "
            "joined_at ASC, id ASC",
            (int(studio_id),),
        )
        return cur.fetchall()

    def leave_session(self, studio_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE studio_sessions SET status='left', left_at=CURRENT_TIMESTAMP, last_seen_at=CURRENT_TIMESTAMP "
            "WHERE studio_id=? AND user_id=?",
            (int(studio_id), int(user_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def create_chat_message(self, studio_id: int, user_id: int, message: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO studio_chat_messages (studio_id, user_id, message) VALUES (?, ?, ?)",
            (int(studio_id), int(user_id), str(message or "")),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_chat_messages(self, studio_id: int, limit: int = 50):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM studio_chat_messages WHERE studio_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(studio_id), max(1, int(limit or 50))),
        )
        return cur.fetchall()
