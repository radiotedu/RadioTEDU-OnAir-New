import json


class ShowRepository:
    _UPDATABLE_COLUMNS = {
        "name", "description", "color", "is_active",
        "intro_path", "outro_path", "break_outro_path", "break_intro_path",
    }

    def __init__(self, conn):
        self.conn = conn

    def get(self, show_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM shows WHERE id = ?", (int(show_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_by_station(self, station_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM shows WHERE station_id = ? AND is_active = 1 ORDER BY name",
            (int(station_id),),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_for_user(self, user_id: int, station_id: int | None = None) -> list[dict]:
        cur = self.conn.cursor()
        if station_id is not None:
            cur.execute(
                "SELECT s.* FROM shows s "
                "JOIN show_assignments sa ON sa.show_id = s.id "
                "WHERE sa.user_id = ? AND s.station_id = ? AND s.is_active = 1 "
                "ORDER BY s.name",
                (int(user_id), int(station_id)),
            )
        else:
            cur.execute(
                "SELECT s.* FROM shows s "
                "JOIN show_assignments sa ON sa.show_id = s.id "
                "WHERE sa.user_id = ? AND s.is_active = 1 "
                "ORDER BY s.name",
                (int(user_id),),
            )
        return [dict(r) for r in cur.fetchall()]

    def create(self, station_id: int, name: str, **kwargs) -> int:
        fields = {"station_id": int(station_id), "name": str(name)}
        for key in ("description", "color", "intro_path", "outro_path",
                     "break_outro_path", "break_intro_path"):
            if key in kwargs and kwargs[key] is not None:
                fields[key] = kwargs[key]
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cur = self.conn.cursor()
        cur.execute(
            f"INSERT INTO shows ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update(self, show_id: int, **fields) -> dict | None:
        updates = {k: v for k, v in fields.items() if k in self._UPDATABLE_COLUMNS}
        if not updates:
            return self.get(show_id)
        updates["updated_at"] = "CURRENT_TIMESTAMP"
        set_parts = []
        values = []
        for k, v in updates.items():
            if v == "CURRENT_TIMESTAMP":
                set_parts.append(f"{k} = CURRENT_TIMESTAMP")
            else:
                set_parts.append(f"{k} = ?")
                values.append(v)
        values.append(int(show_id))
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE shows SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )
        self.conn.commit()
        return self.get(show_id)

    def delete(self, show_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM shows WHERE id = ?", (int(show_id),))
        self.conn.commit()
        return cur.rowcount > 0

    # --- assignments ---

    def assign(
        self, show_id: int, user_id: int, role: str = "dj", permission_keys: set[str] | None = None
    ) -> int:
        cur = self.conn.cursor()
        was_in_transaction = self.conn.in_transaction
        try:
            if permission_keys is None:
                cur.execute(
                    "INSERT INTO show_assignments (show_id, user_id, role) VALUES (?, ?, ?) "
                    "ON CONFLICT(show_id, user_id) DO UPDATE SET role = excluded.role",
                    (int(show_id), int(user_id), str(role)),
                )
            else:
                permission_keys_json = json.dumps(sorted(str(permission) for permission in permission_keys))
                cur.execute(
                    "INSERT INTO show_assignments (show_id, user_id, role, permission_keys_json) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(show_id, user_id) DO UPDATE SET "
                    "role = excluded.role, "
                    "permission_keys_json = excluded.permission_keys_json",
                    (int(show_id), int(user_id), str(role), permission_keys_json),
                )
            if not was_in_transaction:
                self.conn.commit()
        except Exception:
            if not was_in_transaction:
                self.conn.rollback()
            raise
        return int(cur.lastrowid)

    def unassign(self, show_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        was_in_transaction = self.conn.in_transaction
        try:
            cur.execute(
                "DELETE FROM show_assignments WHERE show_id = ? AND user_id = ?",
                (int(show_id), int(user_id)),
            )
            if not was_in_transaction:
                self.conn.commit()
        except Exception:
            if not was_in_transaction:
                self.conn.rollback()
            raise
        return cur.rowcount > 0

    def list_assignments(self, show_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT sa.*, u.username FROM show_assignments sa "
            "JOIN users u ON u.id = sa.user_id "
            "WHERE sa.show_id = ? ORDER BY sa.role, u.username",
            (int(show_id),),
        )
        return [dict(r) for r in cur.fetchall()]

    def is_assigned(self, show_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM show_assignments WHERE show_id = ? AND user_id = ?",
            (int(show_id), int(user_id)),
        )
        return cur.fetchone() is not None
