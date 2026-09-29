import sqlite3
from datetime import datetime, timedelta, timezone

from app.engine import station_worker as station_worker_module
from app.engine.station_worker import StationWorker
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.settings_repo import SettingsRepository


def _break_connection(*, retry_after: str, retry_count: int):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE ad_break_items ("
        "id INTEGER PRIMARY KEY, station_id INTEGER, status TEXT, due_at TEXT, "
        "retry_after TEXT, retry_count INTEGER, priority INTEGER DEFAULT 0)"
    )
    due_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO ad_break_items "
        "(id, station_id, status, due_at, retry_after, retry_count) "
        "VALUES (1, 4, 'pending', ?, ?, ?)",
        (due_at, retry_after, retry_count),
    )
    conn.commit()
    return conn


def _worker(conn, monkeypatch):
    monkeypatch.setattr(
        SettingsRepository,
        "get_station",
        lambda _self, _station_id: {
            "ad_break_advance_notice": "5",
            "ad_break_tolerance_minutes": "10",
        },
    )
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.conn = conn
    worker.events = []
    worker._broadcast_show_event = lambda event, *_args, **_kwargs: worker.events.append(
        event
    )
    return worker


def test_future_retry_deadline_keeps_overdue_break_pending(monkeypatch):
    retry_after = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = _break_connection(retry_after=retry_after, retry_count=1)
    worker = _worker(conn, monkeypatch)

    worker._check_ad_break_timing({"id": 9})

    row = conn.execute("SELECT status FROM ad_break_items WHERE id=1").fetchone()
    assert row["status"] == "pending"
    assert worker.events == []
    assert AdBreakRepository(conn).next_due(4) is None


def test_expired_retry_deadline_keeps_prior_attempt_eligible(monkeypatch):
    retry_after = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = _break_connection(retry_after=retry_after, retry_count=1)
    worker = _worker(conn, monkeypatch)
    station_worker_module._show_notification_sent.pop(("ad_upcoming", 4, 1), None)

    worker._check_ad_break_timing({"id": 9})

    row = conn.execute("SELECT status FROM ad_break_items WHERE id=1").fetchone()
    assert row["status"] == "pending"
    assert AdBreakRepository(conn).next_due(4)["id"] == 1
    assert "ad_break.missed" not in worker.events
    conn.close()
