from __future__ import annotations

import json

from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository


class ReplicationApplier:
    """Applies acknowledged control-plane journal entries on a non-broadcasting standby."""

    def apply_pending(self, limit: int = 250) -> dict:
        init_db()
        conn = get_connection()
        applied = 0
        try:
            rows = conn.execute(
                "SELECT * FROM replication_journal WHERE applied_at IS NULL ORDER BY sequence ASC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
            for row in rows:
                entity_type = str(row["entity_type"])
                payload = json.loads(str(row["payload_json"] or "{}"))
                if entity_type == "stream_config":
                    self._apply_stream_config(conn, payload)
                elif entity_type == "guest_room":
                    self._apply_guest_room(conn, payload)
                elif entity_type == "guest_recording":
                    self._apply_guest_recording(conn, payload)
                elif entity_type == "schedule":
                    self._apply_schedule(conn, str(row["operation"]), payload)
                conn.execute("UPDATE replication_journal SET applied_at=CURRENT_TIMESTAMP WHERE sequence=?", (int(row["sequence"]),))
                applied += 1
            conn.commit()
            return {"applied": applied, "last_sequence": int(rows[-1]["sequence"]) if rows else 0}
        finally:
            conn.close()

    @staticmethod
    def _apply_stream_config(conn, config: dict) -> None:
        station_id = int(config["station_id"])
        repo = StationOutputRepository(conn)
        existing = repo.get_raw(station_id)
        protected_password = str(existing["icecast_password"] or "") if existing else ""
        repo.upsert(
            station_id=station_id,
            local_output_enabled=bool(config.get("local_output_enabled")),
            output_device_id=str(config.get("output_device_id") or ""),
            icecast_enabled=bool(config.get("icecast_enabled", True)),
            icecast_host=str(config.get("icecast_host") or "127.0.0.1"),
            icecast_port=int(config.get("icecast_port") or 8000),
            icecast_mount=str(config.get("icecast_mount") or "/stream"),
            icecast_user=str(config.get("icecast_user") or "source"),
            icecast_password=protected_password,
            output_gain_db=float(config.get("output_gain_db") or 0),
            stream_codec_profile=str(config.get("stream_codec_profile") or "opus_192"),
            stream_bitrate_kbps=int(config.get("stream_bitrate_kbps") or 192),
        )
        SettingsRepository(conn).upsert_station(
            station_id,
            {
                "icecast_tls_enabled": str(bool(config.get("icecast_tls_enabled"))).lower(),
                "icecast_password": "",
            },
        )

    @staticmethod
    def _upsert_row(conn, table: str, row: dict, columns: tuple[str, ...]) -> None:
        values = [row.get(column) for column in columns]
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )

    def _apply_guest_room(self, conn, payload: dict) -> None:
        invite_columns = (
            "id", "studio_id", "station_id", "token_hash", "created_by", "expires_at",
            "redeemed_at", "revoked_at", "created_at",
        )
        session_columns = (
            "id", "invite_id", "studio_id", "station_id", "display_name", "session_token_hash",
            "status", "is_connected", "is_muted", "is_on_air", "gain_db", "connection_quality",
            "admitted_at", "left_at", "last_seen_at", "created_at",
        )
        for row in payload.get("invites", []):
            self._upsert_row(conn, "guest_invites", dict(row), invite_columns)
        for source in payload.get("sessions", []):
            row = dict(source)
            # A recovered WebRTC transport is a new session. Never inherit an
            # on-air bit or a stale connected bit on the standby.
            row["is_connected"] = 0
            row["is_on_air"] = 0
            self._upsert_row(conn, "guest_sessions", row, session_columns)

    def _apply_guest_recording(self, conn, payload: dict) -> None:
        row = dict(payload.get("recording") or {})
        if not row:
            return
        recording_columns = (
            "id", "studio_id", "station_id", "status", "manifest_json", "file_path", "started_by",
            "started_at", "stopped_at", "expires_at", "interruption_reason", "created_at",
        )
        self._upsert_row(conn, "guest_recordings", row, recording_columns)
        for consent in payload.get("consents", []):
            item = dict(consent)
            conn.execute(
                "INSERT INTO guest_recording_consents(recording_id, session_id, decision, decided_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(recording_id, session_id) DO UPDATE SET decision=excluded.decision, decided_at=excluded.decided_at",
                (item.get("recording_id"), item.get("session_id"), item.get("decision"), item.get("decided_at")),
            )

    @staticmethod
    def _apply_schedule(conn, operation: str, payload: dict) -> None:
        schedule_id = int(payload.get("id") or 0)
        if schedule_id <= 0:
            return
        if operation == "delete":
            conn.execute("DELETE FROM schedule_items WHERE id=?", (schedule_id,))
            return
        conn.execute(
            "INSERT INTO schedule_items(id, station_id, track_id, play_at, window_end, event_name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "station_id=excluded.station_id, track_id=excluded.track_id, play_at=excluded.play_at, "
            "window_end=excluded.window_end, event_name=excluded.event_name, status=excluded.status",
            (
                schedule_id,
                int(payload.get("station_id") or 0),
                int(payload.get("track_id") or 0),
                str(payload.get("play_at") or ""),
                payload.get("window_end"),
                str(payload.get("event_name") or ""),
                str(payload.get("status") or "pending"),
            ),
        )


replication_applier = ReplicationApplier()
