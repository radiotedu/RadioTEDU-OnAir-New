import json


class AdCampaignRepository:
    def __init__(self, conn):
        self.conn = conn

    def list_break_sets(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, station_id, name, enabled, payload_json, created_at, updated_at "
            "FROM ad_break_sets WHERE station_id=? ORDER BY id DESC",
            (int(station_id),),
        )
        return cur.fetchall()

    def create_break_set(
        self,
        station_id: int,
        name: str,
        enabled: bool = True,
        payload: dict | None = None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ad_break_sets (station_id, name, enabled, payload_json, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                int(station_id),
                str(name or "").strip(),
                1 if enabled else 0,
                json.dumps(payload or {}),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_break_set(
        self,
        break_set_id: int,
        name: str,
        enabled: bool = True,
        payload: dict | None = None,
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_break_sets SET name=?, enabled=?, payload_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                str(name or "").strip(),
                1 if enabled else 0,
                json.dumps(payload or {}),
                int(break_set_id),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_break_set(self, break_set_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ad_break_sets WHERE id=?", (int(break_set_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def list_campaigns(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, station_id, name, enabled, payload_json, created_at, updated_at "
            "FROM ad_campaigns WHERE station_id=? ORDER BY id DESC",
            (int(station_id),),
        )
        return cur.fetchall()

    def create_campaign(
        self,
        station_id: int,
        name: str,
        enabled: bool = True,
        payload: dict | None = None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ad_campaigns (station_id, name, enabled, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                int(station_id),
                str(name or "").strip(),
                1 if enabled else 0,
                json.dumps(payload or {}),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_campaign(
        self,
        campaign_id: int,
        name: str,
        enabled: bool = True,
        payload: dict | None = None,
    ) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ad_campaigns SET name=?, enabled=?, payload_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                str(name or "").strip(),
                1 if enabled else 0,
                json.dumps(payload or {}),
                int(campaign_id),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_campaign(self, campaign_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ad_campaigns WHERE id=?", (int(campaign_id),))
        self.conn.commit()
        return cur.rowcount > 0
