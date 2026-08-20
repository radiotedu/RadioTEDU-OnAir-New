from app.db import get_connection, init_db
from app.repositories.log_repo import LogRepository


def test_operation_log_retention_prunes_old_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    repo = LogRepository(conn, max_operation_log_rows=3, max_event_rows=10)

    for i in range(5):
        repo.add_operation_log(
            station_id=1,
            message=f"m{i}",
            event_type="http",
            level="info",
        )

    rows = list(repo.list_operation_logs(station_id=1, limit=20))
    assert len(rows) == 3
    messages = [str(row["message"]) for row in rows]
    assert messages == ["m4", "m3", "m2"]


def test_event_retention_prunes_old_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    repo = LogRepository(conn, max_operation_log_rows=10, max_event_rows=2)

    for i in range(4):
        repo.add_event(station_id=1, event_type=f"event-{i}", payload={"n": i})

    rows = list(repo.list_events(station_id=1, limit=20))
    assert len(rows) == 2
    event_types = [str(row["event_type"]) for row in rows]
    assert event_types == ["event-3", "event-2"]
