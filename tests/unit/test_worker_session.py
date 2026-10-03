import sqlite3
from types import SimpleNamespace

import pytest

from app.engine.worker_session import StationWorkerSession


def test_successful_ticks_keep_one_connection_and_close_on_shutdown():
    created = []

    def factory():
        conn = sqlite3.connect(":memory:")
        worker = SimpleNamespace(conn=conn, process_once=lambda: "tick")
        created.append(worker)
        return worker

    session = StationWorkerSession(factory)
    for _ in range(100):
        assert session.process_once() == "tick"
    assert len(created) == 1
    session.close()
    session.close()
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].conn.execute("SELECT 1")


def test_failed_tick_reopens_connection_on_next_tick():
    created = []

    def factory():
        conn = sqlite3.connect(":memory:")

        def tick():
            if len(created) == 1:
                raise sqlite3.OperationalError("database locked")
            return "recovered"

        worker = SimpleNamespace(conn=conn, process_once=tick)
        created.append(worker)
        return worker

    session = StationWorkerSession(factory)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database locked"):
            session.process_once()
        assert session.process_once() == "recovered"
        assert len(created) == 2
        with pytest.raises(sqlite3.ProgrammingError):
            created[0].conn.execute("SELECT 1")
    finally:
        session.close()


def test_committed_ad_state_remains_durable_after_session_restart(tmp_path):
    database = tmp_path / "ads.db"
    bootstrap = sqlite3.connect(database)
    bootstrap.execute("PRAGMA journal_mode=WAL")
    bootstrap.execute("CREATE TABLE ads(id INTEGER PRIMARY KEY, completed INTEGER)")
    bootstrap.commit()
    bootstrap.close()

    def factory():
        conn = sqlite3.connect(database)
        conn.execute("PRAGMA synchronous=FULL")

        def tick():
            conn.execute("INSERT OR REPLACE INTO ads VALUES(1, 1)")
            conn.commit()
            return "ad_completed"

        return SimpleNamespace(conn=conn, process_once=tick)

    session = StationWorkerSession(factory)
    assert session.process_once() == "ad_completed"
    assert session._worker.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    session.close()
    recovered = sqlite3.connect(database)
    try:
        assert recovered.execute("SELECT completed FROM ads WHERE id=1").fetchone()[0] == 1
    finally:
        recovered.close()


@pytest.mark.parametrize("fails", [False, True])
def test_uncommitted_tick_changes_do_not_leak_into_next_tick(tmp_path, fails):
    database = tmp_path / "queue.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE queue(id INTEGER)")
    conn.commit()

    def tick():
        conn.execute("INSERT INTO queue VALUES(1)")
        if fails:
            raise RuntimeError("tick failed before commit")
        return "uncommitted"

    session = StationWorkerSession(lambda: SimpleNamespace(conn=conn, process_once=tick))
    if fails:
        with pytest.raises(RuntimeError):
            session.process_once()
    else:
        assert session.process_once() == "uncommitted"
        assert conn.in_transaction is False
    fresh = sqlite3.connect(database)
    try:
        assert fresh.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0
    finally:
        fresh.close()
        session.close()
