from app.repositories.studio_repo import StudioRepository


class StationRepository:
    def __init__(self, conn):
        self.conn = conn

    def create(self, name: str) -> int:
        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur.execute(
                "INSERT INTO stations (name) VALUES (?)",
                (str(name or "").strip(),),
            )
            station_id = int(cur.lastrowid)
            StudioRepository(self.conn).ensure_default_for_station(
                station_id,
                commit=False,
            )
            self.conn.commit()
            return station_id
        except Exception:
            self.conn.rollback()
            raise

    def list_all(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM stations ORDER BY id ASC")
        return cur.fetchall()

    def update_name(self, station_id: int, name: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE stations SET name=? WHERE id=?",
            (str(name or "").strip(), int(station_id)),
        )
        self.conn.commit()

    def delete(self, station_id: int) -> int | None:
        sid = int(station_id)
        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT id FROM stations WHERE id=?", (sid,))
            if cur.fetchone() is None:
                raise LookupError("station not found")

            rows = list(self.list_all())
            if len(rows) <= 1:
                raise ValueError("cannot delete last station")

            cur.execute(
                "SELECT value FROM system_settings WHERE key='active_station_id'"
            )
            active_row = cur.fetchone()
            active_station_id = int(active_row["value"]) if active_row and str(active_row["value"]).strip() else None

            cur.execute(
                "SELECT value FROM system_settings WHERE key='speaker_monitor_station_id'"
            )
            speaker_row = cur.fetchone()
            speaker_station_id = int(speaker_row["value"]) if speaker_row and str(speaker_row["value"]).strip() else None

            replacement_id = next(
                int(row["id"]) for row in rows if int(row["id"]) != sid
            )

            for table_name in (
                "station_outputs",
                "tracks",
                "queue_items",
                "station_worker_lease",
                "playout_state",
                "schedule_items",
                "ad_break_items",
                "command_outbox",
                "playlists",
                "station_settings",
                "operation_logs",
                "events",
                "program_queue_items",
                "ad_break_sets",
                "ad_campaigns",
                "metadata_rules",
            ):
                cur.execute(f"DELETE FROM {table_name} WHERE station_id=?", (sid,))

            cur.execute("DELETE FROM show_sessions WHERE station_id=?", (sid,))
            cur.execute("DELETE FROM shows WHERE station_id=?", (sid,))
            cur.execute("DELETE FROM stations WHERE id=?", (sid,))

            if active_station_id == sid:
                cur.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES ('active_station_id', ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (str(replacement_id),),
                )
            if speaker_station_id == sid:
                next_speaker = replacement_id if active_station_id == sid else (
                    active_station_id if active_station_id is not None and active_station_id != sid else replacement_id
                )
                cur.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES ('speaker_monitor_station_id', ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (str(next_speaker),),
                )

            self.conn.commit()
            return replacement_id if active_station_id == sid else None
        except Exception:
            self.conn.rollback()
            raise

    def set_active(self, station_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES ('active_station_id', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (str(int(station_id)),),
        )
        self.conn.commit()

    def get_active(self):
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key='active_station_id'")
        row = cur.fetchone()
        if row:
            station_id = int(row["value"])
            cur.execute("SELECT id, name FROM stations WHERE id=?", (station_id,))
            found = cur.fetchone()
            if found:
                return found
        cur.execute("SELECT id, name FROM stations ORDER BY id ASC LIMIT 1")
        return cur.fetchone()
