import sqlite3

from app.engine import broadcast_plan_policy
from app.engine import station_worker as station_worker_module
from app.engine.station_worker import StationWorker


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_break_items (
            id INTEGER PRIMARY KEY, station_id INTEGER, track_id INTEGER,
            status TEXT, dedupe_key TEXT
        );
        CREATE TABLE broadcast_plans (
            id INTEGER PRIMARY KEY, plan_type TEXT, cadence_mode TEXT,
            enabled INTEGER
        );
        CREATE TABLE broadcast_plan_targets (
            plan_id INTEGER, station_id INTEGER, track_id INTEGER, enabled INTEGER
        );
        CREATE TABLE broadcast_plan_occurrences (
            plan_id INTEGER, target_kind TEXT, target_item_id INTEGER
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY, station_id INTEGER, track_type TEXT,
            is_active INTEGER, file_path TEXT, play_count INTEGER,
            last_played_at TEXT
        );
        CREATE TABLE queue_items (
            id INTEGER PRIMARY KEY, station_id INTEGER, track_id INTEGER,
            position INTEGER, status TEXT, dedupe_key TEXT, finished_at TEXT
        );
        """
    )
    return conn


def test_song_cadence_spot_passes_ad_policy_without_occurrence():
    conn = _connection()
    try:
        conn.execute(
            "INSERT INTO broadcast_plans VALUES (1, 'ad', 'songs', 1)"
        )
        conn.execute(
            "INSERT INTO broadcast_plan_targets VALUES (1, 4, 52944, 1)"
        )
        conn.execute(
            "INSERT INTO ad_break_items VALUES "
            "(1, 4, 52944, 'pending', 'broadcast-plan:1:song:4:27')"
        )
        broadcast_plan_policy._AD_CACHE.clear()
        assert broadcast_plan_policy.station_has_planned_ad(conn, 4) is True
        assert broadcast_plan_policy.station_has_planned_ad(conn, 1) is False

        conn.execute("UPDATE broadcast_plans SET enabled=0 WHERE id=1")
        broadcast_plan_policy._AD_CACHE.clear()
        assert broadcast_plan_policy.station_has_planned_ad(conn, 4) is False
    finally:
        broadcast_plan_policy._AD_CACHE.clear()
        conn.close()


def test_sweeper_does_not_pick_ad_when_disabled_or_plan_controls_it(monkeypatch):
    conn = _connection()
    try:
        conn.execute(
            "INSERT INTO tracks VALUES (52944, 4, 'ad', 1, 'C:/ads/powerapp.wav', 0, NULL)"
        )
        worker = StationWorker.__new__(StationWorker)
        worker.conn = conn
        worker.station_id = 4

        monkeypatch.setattr(station_worker_module, "resolve_song_ad_plans", lambda *_: [])
        monkeypatch.setattr(station_worker_module, "station_ads_enabled", lambda *_: False)
        assert worker._pick_random_ad() is None

        monkeypatch.setattr(station_worker_module, "station_ads_enabled", lambda *_: True)
        monkeypatch.setattr(
            station_worker_module,
            "resolve_song_ad_plans",
            lambda *_: [{"plan_id": 1}],
        )
        assert worker._pick_random_ad() is None

        monkeypatch.setattr(station_worker_module, "resolve_song_ad_plans", lambda *_: [])
        assert worker._pick_random_ad() == {"track_id": 52944}
    finally:
        conn.close()


def test_pending_legacy_ads_are_retired_without_touching_music(monkeypatch):
    conn = _connection()
    try:
        conn.execute(
            "INSERT INTO tracks VALUES (52944, 4, 'ad', 1, 'C:/ads/powerapp.wav', 0, NULL)"
        )
        conn.execute(
            "INSERT INTO tracks VALUES (100, 4, 'music', 1, 'C:/music/song.wav', 0, NULL)"
        )
        conn.executemany(
            "INSERT INTO queue_items "
            "(id, station_id, track_id, position, status, dedupe_key) "
            "VALUES (?, 4, ?, ?, 'pending', ?)",
            [
                (1, 52944, 1, "ad:52944:1"),
                (2, 100, 2, "music:100:2"),
                (3, 52944, 3, "manual:52944:3"),
            ],
        )
        worker = StationWorker.__new__(StationWorker)
        worker.conn = conn
        worker.station_id = 4
        monkeypatch.setattr(station_worker_module, "station_ads_enabled", lambda *_: False)
        monkeypatch.setattr(station_worker_module, "resolve_song_ad_plans", lambda *_: [])

        assert worker._remove_unplanned_pending_ads() == 1
        assert [
            tuple(row)
            for row in conn.execute("SELECT id, status FROM queue_items ORDER BY id")
        ] == [(1, "failed"), (2, "pending"), (3, "pending")]
    finally:
        conn.close()
