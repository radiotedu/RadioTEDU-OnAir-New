from datetime import datetime, timedelta, timezone
import threading
import sqlite3

import pytest

from app.db import get_connection, init_db
from app.engine.lease import LeaseService


class CountingConnection(sqlite3.Connection):
    commits = 0

    def commit(self):
        self.commits += 1
        return super().commit()


@pytest.fixture
def lease_connection():
    conn = sqlite3.connect(":memory:", factory=CountingConnection)
    conn.execute("CREATE TABLE station_worker_lease (station_id INTEGER PRIMARY KEY, "
                 "worker_id TEXT, lease_expires_at TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    yield conn
    conn.close()


def test_valid_persisted_owner_does_not_flush_every_tick(lease_connection):
    service = LeaseService(lease_connection, lease_seconds=30)
    assert service.try_acquire(1, "owner") is True
    expiry = lease_connection.execute("SELECT lease_expires_at FROM station_worker_lease").fetchone()[0]
    lease_connection.commits = 0
    writes = lease_connection.total_changes
    for _ in range(100):
        assert service.try_acquire(1, "owner") is True
    assert lease_connection.commits == 0
    assert lease_connection.total_changes == writes
    assert lease_connection.execute("SELECT lease_expires_at FROM station_worker_lease").fetchone()[0] == expiry


def test_lease_renews_before_expiry(lease_connection):
    service = LeaseService(lease_connection, lease_seconds=30)
    service.try_acquire(1, "owner")
    old_expiry = (datetime.now(timezone.utc) + timedelta(seconds=19)).isoformat()
    lease_connection.execute("UPDATE station_worker_lease SET lease_expires_at=?", (old_expiry,))
    lease_connection.commit()
    lease_connection.commits = 0
    assert service.try_acquire(1, "owner") is True
    assert lease_connection.commits == 1
    new_expiry = lease_connection.execute("SELECT lease_expires_at FROM station_worker_lease").fetchone()[0]
    assert datetime.fromisoformat(new_expiry) > datetime.fromisoformat(old_expiry)


def test_skip_renewal_rechecks_persisted_owner(lease_connection):
    service = LeaseService(lease_connection, lease_seconds=30)
    service.try_acquire(1, "owner")
    lease_connection.execute("UPDATE station_worker_lease SET worker_id='replacement'")
    lease_connection.commit()
    lease_connection.commits = 0
    assert service.try_acquire(1, "owner") is False
    assert lease_connection.commits == 0


@pytest.mark.parametrize("expiry", ["broken timestamp", "2000-01-01T00:00:00Z"])
def test_invalid_or_expired_lease_is_durably_reclaimed(lease_connection, expiry):
    lease_connection.execute("INSERT INTO station_worker_lease VALUES (1, 'old', ?, '')", (expiry,))
    lease_connection.commit()
    lease_connection.commits = 0
    assert LeaseService(lease_connection).try_acquire(1, "replacement") is True
    assert lease_connection.commits == 1
    assert lease_connection.execute("SELECT worker_id FROM station_worker_lease").fetchone()[0] == "replacement"


def test_same_owner_unreasonable_clock_expiry_is_repaired(lease_connection):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    lease_connection.execute("INSERT INTO station_worker_lease VALUES (1, 'owner', ?, '')", (future.isoformat(),))
    lease_connection.commit()
    lease_connection.commits = 0
    assert LeaseService(lease_connection, lease_seconds=30).try_acquire(1, "owner") is True
    assert lease_connection.commits == 1
    expiry = lease_connection.execute("SELECT lease_expires_at FROM station_worker_lease").fetchone()[0]
    assert datetime.fromisoformat(expiry) < future


def test_second_worker_cannot_take_active_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    svc = LeaseService(conn, lease_seconds=30)
    assert svc.try_acquire(station_id=1, worker_id="w1") is True
    assert svc.try_acquire(station_id=1, worker_id="w2") is False
    conn.close()


def test_concurrent_workers_cannot_both_acquire_new_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    barrier = threading.Barrier(2)
    results = []

    def attempt(worker_id: str):
        conn = get_connection()
        try:
            barrier.wait(timeout=2.0)
            results.append(LeaseService(conn, lease_seconds=30).try_acquire(1, worker_id))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=attempt, args=("worker-a",)),
        threading.Thread(target=attempt, args=("worker-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]


def test_unreasonable_future_lease_does_not_fence_playout_after_clock_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    future = datetime.now(timezone.utc) + timedelta(days=1)
    conn.execute(
        "INSERT INTO station_worker_lease (station_id, worker_id, lease_expires_at) "
        "VALUES (?, ?, ?)",
        (1, "stale-worker", future.isoformat()),
    )
    conn.commit()

    assert LeaseService(conn, lease_seconds=30).try_acquire(1, "new-worker") is True
    owner = conn.execute(
        "SELECT worker_id FROM station_worker_lease WHERE station_id=1"
    ).fetchone()["worker_id"]
    assert owner == "new-worker"
    conn.close()
