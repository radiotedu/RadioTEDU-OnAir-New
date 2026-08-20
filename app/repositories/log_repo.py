import json

from app.config import MAX_EVENT_ROWS, MAX_OPERATION_LOG_ROWS


class LogRepository:
    def __init__(
        self,
        conn,
        max_operation_log_rows: int = MAX_OPERATION_LOG_ROWS,
        max_event_rows: int = MAX_EVENT_ROWS,
    ):
        self.conn = conn
        self.max_operation_log_rows = max(0, int(max_operation_log_rows))
        self.max_event_rows = max(0, int(max_event_rows))

    def _prune_table(self, table_name: str, max_rows: int) -> None:
        if max_rows <= 0:
            return
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT id FROM {table_name} ORDER BY id DESC LIMIT 1 OFFSET ?",
            (max(0, int(max_rows) - 1),),
        )
        row = cur.fetchone()
        if not row:
            return
        cutoff_id = int(row["id"])
        cur.execute(f"DELETE FROM {table_name} WHERE id < ?", (cutoff_id,))

    def add_operation_log(
        self,
        station_id: int | None,
        message: str,
        event_type: str = "operation",
        level: str = "info",
        payload: dict | None = None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO operation_logs (station_id, level, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                int(station_id) if station_id is not None else None,
                str(level or "info"),
                str(event_type or "operation"),
                str(message or ""),
                json.dumps(payload or {}),
            ),
        )
        self._prune_table("operation_logs", self.max_operation_log_rows)
        self.conn.commit()
        return int(cur.lastrowid)

    def list_operation_logs(self, station_id: int | None = None, limit: int = 200):
        safe_limit = max(1, min(int(limit), 2000))
        cur = self.conn.cursor()
        if station_id is None:
            cur.execute(
                "SELECT id, station_id, level, event_type, message, payload_json, created_at "
                "FROM operation_logs ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            )
        else:
            cur.execute(
                "SELECT id, station_id, level, event_type, message, payload_json, created_at "
                "FROM operation_logs WHERE station_id=? ORDER BY id DESC LIMIT ?",
                (int(station_id), safe_limit),
            )
        return cur.fetchall()

    def add_event(self, station_id: int, event_type: str, payload: dict | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO events (station_id, event_type, payload_json) VALUES (?, ?, ?)",
            (int(station_id), str(event_type or "generic"), json.dumps(payload or {})),
        )
        self._prune_table("events", self.max_event_rows)
        self.conn.commit()
        return int(cur.lastrowid)

    def list_events(self, station_id: int | None = None, limit: int = 200):
        safe_limit = max(1, min(int(limit), 2000))
        cur = self.conn.cursor()
        if station_id is None:
            cur.execute(
                "SELECT id, station_id, event_type, payload_json, created_at "
                "FROM events ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            )
        else:
            cur.execute(
                "SELECT id, station_id, event_type, payload_json, created_at "
                "FROM events WHERE station_id=? ORDER BY id DESC LIMIT ?",
                (int(station_id), safe_limit),
            )
        return cur.fetchall()

    def delete_event(self, event_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM events WHERE id=?", (int(event_id),))
        self.conn.commit()
        return cur.rowcount > 0
