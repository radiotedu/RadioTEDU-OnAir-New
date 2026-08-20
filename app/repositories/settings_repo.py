DEFAULT_SYSTEM_SETTINGS = {
    "ui_language": "en-US",
    "default_crossfade_seconds": "3.0",
    "operation_logs_enabled": "true",
    "auto_scan_on_startup": "false",
    "display_brand_name": "RadioTEDU OnAir",
    "active_station_id": "1",
    "speaker_monitor_station_id": "1",
}


class SettingsRepository:
    def __init__(self, conn):
        self.conn = conn

    def upsert_system(self, values: dict[str, str]) -> None:
        cur = self.conn.cursor()
        for key, value in dict(values or {}).items():
            cur.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (str(key), str(value)),
            )
        self.conn.commit()

    def get_system(self) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM system_settings")
        output = dict(DEFAULT_SYSTEM_SETTINGS)
        output.update({str(row["key"]): str(row["value"]) for row in cur.fetchall()})
        return output

    def ensure_system_defaults(
        self,
        values: dict[str, str] | None = None,
    ) -> dict[str, str]:
        cur = self.conn.cursor()
        defaults = dict(DEFAULT_SYSTEM_SETTINGS)
        defaults.update({str(key): str(value) for key, value in dict(values or {}).items()})
        for key, value in defaults.items():
            cur.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO NOTHING",
                (str(key), str(value)),
            )
        self.conn.commit()
        return self.get_system()

    def upsert_station(self, station_id: int, values: dict[str, str]) -> None:
        cur = self.conn.cursor()
        sid = int(station_id)
        for key, value in dict(values or {}).items():
            cur.execute(
                "INSERT INTO station_settings (station_id, key, value, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(station_id, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (sid, str(key), str(value)),
            )
        self.conn.commit()

    def get_station(self, station_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT key, value FROM station_settings WHERE station_id=?",
            (int(station_id),),
        )
        return {str(row["key"]): str(row["value"]) for row in cur.fetchall()}
