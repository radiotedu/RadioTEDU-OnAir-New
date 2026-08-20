import socket

from app.security.credential_vault import (
    is_credential_reference,
    resolve_station_icecast_password,
    store_station_icecast_password,
)


class StationOutputRepository:
    def __init__(self, conn):
        self.conn = conn

    def upsert(
        self,
        station_id: int,
        local_output_enabled: bool,
        output_device_id: str,
        icecast_enabled: bool,
        icecast_host: str,
        icecast_port: int,
        icecast_mount: str,
        icecast_user: str,
        icecast_password: str,
        output_gain_db: float = 0.0,
        stream_codec_profile: str = "opus_192",
        stream_bitrate_kbps: int = 192,
        source_protocol: str = "icecast",
    ) -> None:
        existing = self.get_raw(station_id)
        stored_password = str(icecast_password or "")
        if not stored_password and existing is not None:
            stored_password = str(existing["icecast_password"] or "")
        if stored_password and not is_credential_reference(stored_password):
            stored_password = store_station_icecast_password(
                station_id, stored_password
            )
        normalized_protocol = str(source_protocol or "icecast").strip().lower()
        if normalized_protocol not in {"icecast", "shoutcast"}:
            raise ValueError("source_protocol must be icecast or shoutcast")
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO station_outputs (station_id, local_output_enabled, output_device_id, icecast_enabled, icecast_host, icecast_port, icecast_mount, icecast_user, icecast_password, output_gain_db, stream_codec_profile, stream_bitrate_kbps, source_protocol) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET "
            "local_output_enabled=excluded.local_output_enabled, "
            "output_device_id=excluded.output_device_id, "
            "icecast_enabled=excluded.icecast_enabled, "
            "icecast_host=excluded.icecast_host, "
            "icecast_port=excluded.icecast_port, "
            "icecast_mount=excluded.icecast_mount, "
            "icecast_user=excluded.icecast_user, "
            "icecast_password=excluded.icecast_password, "
            "output_gain_db=excluded.output_gain_db, "
            "stream_codec_profile=excluded.stream_codec_profile, "
            "stream_bitrate_kbps=excluded.stream_bitrate_kbps, "
            "source_protocol=excluded.source_protocol",
            (
                station_id,
                int(local_output_enabled),
                output_device_id,
                int(icecast_enabled),
                icecast_host,
                icecast_port,
                icecast_mount,
                icecast_user,
                stored_password,
                output_gain_db,
                str(stream_codec_profile or "opus_192"),
                int(stream_bitrate_kbps or 192),
                normalized_protocol,
            ),
        )
        self.conn.commit()

    def get_raw(self, station_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM station_outputs WHERE station_id=?", (station_id,))
        return cur.fetchone()

    def get(self, station_id: int):
        row = self.get_raw(station_id)
        if row is None:
            return None
        payload = dict(row)
        payload["icecast_password"] = resolve_station_icecast_password(
            station_id,
            str(row["icecast_password"] or ""),
        )
        return payload

    def list_active_local_output_assignments(
        self,
        exclude_station_id: int | None = None,
    ) -> list[dict[str, str | int]]:
        cur = self.conn.cursor()
        query = (
            "SELECT station_id, output_device_id FROM station_outputs "
            "WHERE local_output_enabled=1 AND TRIM(output_device_id) <> ''"
        )
        params: list[int] = []
        if exclude_station_id is not None:
            query += " AND station_id <> ?"
            params.append(int(exclude_station_id))
        query += " ORDER BY station_id ASC"
        cur.execute(query, tuple(params))
        return [
            {
                "station_id": int(row["station_id"]),
                "output_device_id": str(row["output_device_id"] or "").strip(),
            }
            for row in cur.fetchall()
        ]

    def count_active_local_outputs(self) -> int:
        return len(self.list_active_local_output_assignments())

    def find_active_stream_conflict(
        self,
        *,
        station_id: int,
        host: str,
        port: int,
        mount: str,
        source_protocol: str = "icecast",
    ):
        """Return another station claiming the same enabled source endpoint."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT station_id, icecast_host, icecast_port, icecast_mount "
            "FROM station_outputs "
            "WHERE station_id <> ? AND icecast_enabled = 1 "
            "AND icecast_port = ? AND icecast_mount = ? "
            "AND LOWER(TRIM(source_protocol)) = LOWER(TRIM(?)) "
            "ORDER BY station_id ASC",
            (
                int(station_id),
                int(port),
                str(mount or "").strip(),
                str(source_protocol or "icecast").strip(),
            ),
        )
        candidates = cur.fetchall()
        requested_host = str(host or "").strip().casefold()
        for row in candidates:
            if str(row["icecast_host"] or "").strip().casefold() == requested_host:
                return row

        # Operators commonly use a private IP in one screen and the matching
        # DNS name in another. Treat aliases resolving to the same origin as
        # one destination so two workers cannot fight over a mount and create
        # the exact disconnect/reconnect loop this guard is meant to prevent.
        def resolved_addresses(value: str) -> set[str]:
            try:
                return {
                    str(item[4][0]).casefold()
                    for item in socket.getaddrinfo(value, int(port), type=socket.SOCK_STREAM)
                }
            except OSError:
                return set()

        requested_addresses = resolved_addresses(requested_host)
        if not requested_addresses:
            return None
        for row in candidates:
            if requested_addresses.intersection(
                resolved_addresses(str(row["icecast_host"] or "").strip())
            ):
                return row
        return None
