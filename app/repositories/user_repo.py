import hashlib


def _hash_refresh_token(refresh_token: str) -> str:
    return hashlib.sha256(str(refresh_token or "").encode("utf-8")).hexdigest()


class UserRepository:
    def __init__(self, conn):
        self.conn = conn

    def create_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
        *,
        commit: bool = True,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO users (username, display_name, password_hash, role) "
            "VALUES (?, ?, ?, ?)",
            (str(username), str(display_name), str(password_hash), str(role)),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def get_user_by_id(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=?", (int(user_id),))
        return cur.fetchone()

    def get_user_by_username(self, username: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", (str(username),))
        return cur.fetchone()

    def list_users(self, include_inactive: bool = False):
        cur = self.conn.cursor()
        if include_inactive:
            cur.execute("SELECT * FROM users ORDER BY id ASC")
        else:
            cur.execute("SELECT * FROM users WHERE is_active=1 ORDER BY id ASC")
        return cur.fetchall()

    def update_user(self, user_id: int, commit: bool = True, **fields) -> bool:
        updates = {str(key): value for key, value in dict(fields).items() if value is not None}
        if not updates:
            return False
        clauses = [f"{key}=?" for key in updates]
        values = list(updates.values())
        values.append(int(user_id))
        cur = self.conn.cursor()
        cur.execute(
            f"UPDATE users SET {', '.join(clauses)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            tuple(values),
        )
        if commit:
            self.conn.commit()
        return cur.rowcount > 0

    def deactivate_user(self, user_id: int) -> bool:
        return self.update_user(user_id, is_active=0)

    def update_last_login(self, user_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE users SET last_login_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(user_id),),
        )
        self.conn.commit()


class SessionRepository:
    def __init__(self, conn):
        self.conn = conn

    def create_session(
        self,
        user_id: int,
        refresh_token: str,
        device_info: str | None,
        ip_address: str | None,
        expires_at: str,
    ) -> int:
        token_hash = _hash_refresh_token(refresh_token)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO user_sessions (user_id, refresh_token, device_info, ip_address, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                int(user_id),
                token_hash,
                device_info,
                ip_address,
                str(expires_at),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_session_by_token(self, refresh_token: str):
        token_hash = _hash_refresh_token(refresh_token)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM user_sessions WHERE refresh_token=?",
            (token_hash,),
        )
        row = cur.fetchone()
        if row is not None:
            return row
        # Backward-compatible fallback for legacy plaintext session rows.
        cur.execute(
            "SELECT * FROM user_sessions WHERE refresh_token=?",
            (str(refresh_token),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            cur.execute(
                "UPDATE user_sessions SET refresh_token=? WHERE id=?",
                (token_hash, int(row["id"])),
            )
            self.conn.commit()
        except Exception:
            pass
        return row

    def revoke_session(self, session_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE user_sessions SET revoked=1 WHERE id=?",
            (int(session_id),),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def revoke_all_user_sessions(self, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE user_sessions SET revoked=1 WHERE user_id=?",
            (int(user_id),),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def cleanup_expired(self) -> int:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM user_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
        self.conn.commit()
        return int(cur.rowcount or 0)


class ApiKeyRepository:
    def __init__(self, conn):
        self.conn = conn

    def create_key(
        self,
        user_id: int,
        key_hash: str,
        name: str,
        permissions: str,
        expires_at: str | None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO api_keys (user_id, key_hash, name, permissions, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(user_id), str(key_hash), str(name), str(permissions), expires_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_key_by_hash(self, key_hash: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM api_keys WHERE key_hash=?", (str(key_hash),))
        return cur.fetchone()

    def list_keys(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM api_keys WHERE user_id=? ORDER BY id ASC", (int(user_id),))
        return cur.fetchall()

    def revoke_key(self, key_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (int(key_id),))
        self.conn.commit()
        return cur.rowcount > 0
