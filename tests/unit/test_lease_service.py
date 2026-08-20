from datetime import datetime, timedelta, timezone
import threading

from app.db import get_connection, init_db
from app.engine.lease import LeaseService


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
