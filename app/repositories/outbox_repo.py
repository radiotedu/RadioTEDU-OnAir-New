import json


class OutboxRepository:
    def __init__(self, conn):
        self.conn = conn

    def enqueue(self, station_id: int, command_type: str, payload: dict) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO command_outbox (station_id, command_type, payload_json, status) VALUES (?, ?, ?, 'pending')",
            (station_id, command_type, json.dumps(payload)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_next(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM command_outbox WHERE station_id=? AND status='pending' ORDER BY id ASC LIMIT 1",
            (station_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE command_outbox SET status='processing', attempt_count=attempt_count+1 WHERE id=?",
            (row["id"],),
        )
        self.conn.commit()
        return row

    def mark_done(self, cmd_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE command_outbox SET status='done' WHERE id=?", (cmd_id,))
        self.conn.commit()

    def mark_failed(self, cmd_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE command_outbox SET status='failed' WHERE id=?", (cmd_id,))
        self.conn.commit()

    def list_recent(self, station_id: int | None = None, status: str = "", limit: int = 50):
        safe_limit = max(1, min(int(limit), 500))
        cur = self.conn.cursor()
        clauses = []
        params: list = []
        if station_id is not None:
            clauses.append("station_id=?")
            params.append(int(station_id))
        if status:
            clauses.append("status=?")
            params.append(str(status))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT id, station_id, command_type, payload_json, status, attempt_count, available_at, created_at "
            f"FROM command_outbox {where_sql} ORDER BY id DESC LIMIT ?"
        )
        params.append(safe_limit)
        cur.execute(sql, tuple(params))
        return cur.fetchall()
