class RbacRepository:
    _ROLE_TEMPLATE_FIELDS = {"name", "description", "is_system", "is_active"}

    def __init__(self, conn):
        self.conn = conn

    def _write_value(self, operation):
        cur = self.conn.cursor()
        if self.conn.in_transaction:
            savepoint = "rbac_write_value"
            try:
                cur.execute(f"SAVEPOINT {savepoint}")
                result = operation(cur)
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                return result
            except Exception:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

        try:
            self.conn.execute("BEGIN")
            result = operation(cur)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def _replace_values(self, delete_sql: str, delete_params: tuple, insert_sql: str, rows) -> None:
        cur = self.conn.cursor()
        if self.conn.in_transaction:
            savepoint = "rbac_replace_values"
            try:
                cur.execute(f"SAVEPOINT {savepoint}")
                cur.execute(delete_sql, delete_params)
                if rows:
                    cur.executemany(insert_sql, rows)
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return

        try:
            self.conn.execute("BEGIN")
            cur.execute(delete_sql, delete_params)
            if rows:
                cur.executemany(insert_sql, rows)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def create_role_template(self, name: str, description: str, is_system: bool) -> int:
        def operation(cur):
            cur.execute(
                "INSERT INTO role_templates (name, description, is_system) VALUES (?, ?, ?)",
                (str(name), str(description), int(bool(is_system))),
            )
            return int(cur.lastrowid)

        return self._write_value(operation)

    def update_role_template(self, role_template_id: int, **fields) -> bool:
        updates = {
            key: value
            for key, value in dict(fields).items()
            if key in self._ROLE_TEMPLATE_FIELDS and value is not None
        }
        if not updates:
            return False

        set_parts = []
        values = []
        for key, value in updates.items():
            set_parts.append(f"{key} = ?")
            if key == "is_system" or key == "is_active":
                values.append(int(bool(value)))
            else:
                values.append(str(value))
        set_parts.append("updated_at = CURRENT_TIMESTAMP")
        values.append(int(role_template_id))

        def operation(cur):
            cur.execute(
                f"UPDATE role_templates SET {', '.join(set_parts)} WHERE id = ?",
                tuple(values),
            )
            return cur.rowcount > 0

        return self._write_value(operation)

    def get_role_template(self, role_template_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM role_templates WHERE id = ?", (int(role_template_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_role_templates(self, include_inactive: bool = False) -> list[dict]:
        cur = self.conn.cursor()
        if include_inactive:
            cur.execute("SELECT * FROM role_templates ORDER BY name ASC, id ASC")
        else:
            cur.execute(
                "SELECT * FROM role_templates WHERE is_active = 1 ORDER BY name ASC, id ASC"
            )
        return [dict(row) for row in cur.fetchall()]

    def replace_role_permissions(self, role_template_id: int, permission_keys: set[str]) -> None:
        rows = [
            (int(role_template_id), str(permission))
            for permission in sorted(permission_keys)
        ]
        self._replace_values(
            "DELETE FROM role_template_permissions WHERE role_template_id = ?",
            (int(role_template_id),),
            "INSERT INTO role_template_permissions (role_template_id, permission_key) VALUES (?, ?)",
            rows,
        )

    def list_role_permissions(self, role_template_id: int) -> set[str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT permission_key FROM role_template_permissions WHERE role_template_id = ?",
            (int(role_template_id),),
        )
        return {str(row[0]) for row in cur.fetchall()}

    def replace_user_roles(self, user_id: int, role_template_ids: set[int]) -> None:
        rows = [
            (int(user_id), int(role_template_id))
            for role_template_id in sorted(role_template_ids)
        ]
        self._replace_values(
            "DELETE FROM user_role_assignments WHERE user_id = ?",
            (int(user_id),),
            "INSERT INTO user_role_assignments (user_id, role_template_id) VALUES (?, ?)",
            rows,
        )

    def list_user_role_ids(self, user_id: int) -> set[int]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT role_template_id FROM user_role_assignments WHERE user_id = ?",
            (int(user_id),),
        )
        return {int(row[0]) for row in cur.fetchall()}

    def list_effective_global_permissions(self, user_id: int) -> set[str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT rp.permission_key "
            "FROM user_role_assignments ura "
            "JOIN role_template_permissions rp ON rp.role_template_id = ura.role_template_id "
            "JOIN role_templates rt ON rt.id = ura.role_template_id "
            "WHERE ura.user_id = ? AND rt.is_active = 1",
            (int(user_id),),
        )
        return {str(row[0]) for row in cur.fetchall()}

    def replace_show_permissions(
        self, show_id: int, user_id: int, permission_keys: set[str]
    ) -> None:
        rows = [
            (int(show_id), int(user_id), str(permission))
            for permission in sorted(permission_keys)
        ]
        self._replace_values(
            "DELETE FROM show_assignment_permissions WHERE show_id = ? AND user_id = ?",
            (int(show_id), int(user_id)),
            "INSERT INTO show_assignment_permissions (show_id, user_id, permission_key) "
            "VALUES (?, ?, ?)",
            rows,
        )

    def list_show_permissions(self, show_id: int, user_id: int) -> set[str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT permission_key FROM show_assignment_permissions "
            "WHERE show_id = ? AND user_id = ?",
            (int(show_id), int(user_id)),
        )
        return {str(row[0]) for row in cur.fetchall()}

    def list_user_show_permissions(self, user_id: int) -> dict[int, set[str]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT sa.show_id, sap.permission_key "
            "FROM show_assignments sa "
            "LEFT JOIN show_assignment_permissions sap "
            "ON sap.show_id = sa.show_id AND sap.user_id = sa.user_id "
            "WHERE sa.user_id = ? "
            "ORDER BY sa.show_id ASC, sap.permission_key ASC",
            (int(user_id),),
        )
        permissions: dict[int, set[str]] = {}
        for row in cur.fetchall():
            show_id = int(row["show_id"])
            permissions.setdefault(show_id, set())
            permission_key = row["permission_key"]
            if permission_key is not None:
                permissions[show_id].add(str(permission_key))
        return permissions
