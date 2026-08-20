from datetime import datetime, timedelta, timezone


class LeaseService:
    def __init__(self, conn, lease_seconds: int = 30):
        self.conn = conn
        self.lease_seconds = lease_seconds

    def try_acquire(self, station_id: int, worker_id: str) -> bool:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self.lease_seconds)
        # A large future expiry can survive a host clock correction and fence
        # the only playout worker for hours.  Same-host leases should never be
        # farther ahead than a small multiple of their configured lifetime.
        latest_reasonable_expiry = now + timedelta(
            seconds=max(1, int(self.lease_seconds)) * 4
        )
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO station_worker_lease "
            "(station_id, worker_id, lease_expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET "
            "worker_id=excluded.worker_id, "
            "lease_expires_at=excluded.lease_expires_at, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE station_worker_lease.worker_id=excluded.worker_id "
            "OR julianday(station_worker_lease.lease_expires_at) IS NULL "
            "OR julianday(station_worker_lease.lease_expires_at) <= julianday(?) "
            "OR julianday(station_worker_lease.lease_expires_at) > julianday(?)",
            (
                int(station_id),
                str(worker_id),
                expires.isoformat(),
                now.isoformat(),
                latest_reasonable_expiry.isoformat(),
            ),
        )
        acquired = int(cur.rowcount or 0) == 1
        self.conn.commit()
        return acquired
