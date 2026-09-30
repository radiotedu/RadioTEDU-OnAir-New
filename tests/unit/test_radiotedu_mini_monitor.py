import sqlite3
import time

from tools import radiotedu_mini_monitor as monitor


def _ad_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, station_id INTEGER, title TEXT, artist TEXT,
            duration REAL, track_type TEXT, file_path TEXT, is_active INTEGER
        );
        CREATE TABLE ad_break_items (
            id INTEGER PRIMARY KEY, station_id INTEGER, track_id INTEGER,
            due_at TEXT, status TEXT, priority INTEGER, started_at TEXT,
            finished_at TEXT, dedupe_key TEXT
        );
        CREATE TABLE broadcast_plans (
            id INTEGER PRIMARY KEY, name TEXT, plan_type TEXT, cadence_mode TEXT,
            enabled INTEGER, source_station_id INTEGER, starts_on TEXT, ends_on TEXT,
            weekdays_json TEXT, local_start TEXT, local_end TEXT, timezone TEXT,
            repeat_every_songs INTEGER, priority INTEGER, created_at TEXT
        );
        CREATE TABLE broadcast_plan_targets (
            plan_id INTEGER, station_id INTEGER, track_id INTEGER, enabled INTEGER
        );
        CREATE TABLE queue_items (
            id INTEGER PRIMARY KEY, station_id INTEGER, track_id INTEGER,
            status TEXT, finished_at TEXT
        );
        INSERT INTO tracks VALUES
            (1, 4, 'PowerApp', 'Sponsor', 20, 'ad', 'E:/ads/powerapp.mp3', 1),
            (2, 4, 'Song', 'Artist', 200, 'music', 'E:/pop/song.mp3', 1);
        INSERT INTO broadcast_plans VALUES
            (7, 'PowerApp cadence', 'ad', 'songs', 1, 1, '2000-01-01', '2100-12-31',
             '[1,2,3,4,5,6,7]', '00:00', '00:00', 'UTC', 10, 100, '2000-01-01 00:00:00');
        INSERT INTO broadcast_plan_targets VALUES (7, 4, 1, 1);
        """
    )
    for _ in range(10):
        conn.execute(
            "INSERT INTO queue_items (station_id, track_id, status, finished_at) "
            "VALUES (4, 2, 'done', CURRENT_TIMESTAMP)"
        )
    return conn


def test_monitor_distinguishes_stopped_producer_from_output_feed():
    base = {"updated_epoch": time.time(), "running": True, "runtime_status": {
        "program_running": False, "output_feed_active": False,
    }}

    assert monitor._classify(base, None, None) == ("ÜRETİCİ DURDU", "bad")
    base["runtime_status"]["output_feed_active"] = True
    assert monitor._classify(base, None, None) == ("SES KAYNAĞI DURDU", "warn")


def test_monitor_reports_due_plan_without_materialized_ad_as_unmaterialized():
    conn = _ad_db()
    try:
        result = monitor._ad_plan_snapshot(conn, 4)
    finally:
        conn.close()

    assert result["state"] == "unmaterialized"
    assert result["remaining"] == 0
    assert result["name"] == "PowerApp cadence"


def test_monitor_reports_ad_as_queued_only_for_real_pending_row():
    conn = _ad_db()
    conn.execute(
        "INSERT INTO ad_break_items "
        "(station_id, track_id, due_at, status, priority, dedupe_key) "
        "VALUES (4, 1, CURRENT_TIMESTAMP, 'pending', 100, 'broadcast-plan:7:song:4:1')"
    )
    try:
        result = monitor._ad_plan_snapshot(conn, 4)
    finally:
        conn.close()

    assert result["state"] == "pending"
    assert result["title"] == "PowerApp"


def test_monitor_reports_disabled_station_target_as_inactive():
    conn = _ad_db()
    conn.execute("UPDATE broadcast_plan_targets SET enabled=0 WHERE plan_id=7 AND station_id=4")
    try:
        result = monitor._ad_plan_snapshot(conn, 4)
    finally:
        conn.close()

    assert result == {"state": "inactive", "reason": "target_disabled"}
