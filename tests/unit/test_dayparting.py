import array
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.audio.bpm_analyzer import SAMPLE_RATE, estimate_bpm_from_samples
from app.db import _migrate_daypart_rules, get_connection, init_db
from app.engine import station_worker as station_worker_module
from app.engine.station_worker import StationWorker
from app.services.dayparting import (
    DaypartRule,
    active_daypart,
    default_rules_for_station,
    station_profile,
)


def _add_station(conn, name: str) -> int:
    cursor = conn.execute("INSERT INTO stations(name) VALUES (?)", (name,))
    conn.commit()
    return int(cursor.lastrowid)


def test_default_profiles_include_sleep_nights_for_lofi_and_jazz():
    assert station_profile("RadioTEDU Energize") == "energize"
    assert station_profile("RadioTEDU Classical") == "classic"
    lofi = default_rules_for_station("RadioTEDU Lo-Fi")
    jazz = default_rules_for_station("RadioTEDU Jazz")
    assert len(lofi) == len(jazz) == 49
    monday_lofi = [rule for rule in lofi if rule.day_of_week == 0]
    monday_jazz = [rule for rule in jazz if rule.day_of_week == 0]
    assert [(rule.name, rule.min_bpm, rule.max_bpm) for rule in monday_lofi[-2:]] == [
        ("Sleep Tapes", 55.0, 78.0),
        ("Deep Sleep Loops", 45.0, 70.0),
    ]
    assert [(rule.name, rule.min_bpm, rule.max_bpm) for rule in monday_jazz[-2:]] == [
        ("Midnight Ballads", 55.0, 85.0),
        ("Dreamland Jazz", 45.0, 72.0),
    ]


def test_cross_midnight_default_resolves_in_istanbul():
    init_db()
    conn = get_connection()
    station_id = _add_station(conn, "RadioTEDU Lo-Fi")
    at = datetime(2026, 8, 3, 23, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert active_daypart(conn, station_id, at=at).name == "Sleep Tapes"
    at = datetime(2026, 8, 4, 1, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert active_daypart(conn, station_id, at=at).name == "Sleep Tapes"
    at = datetime(2026, 8, 4, 3, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    assert active_daypart(conn, station_id, at=at).name == "Quiet Hours"
    conn.close()


def test_every_station_has_seven_distinct_named_days_with_stable_bpm_slots():
    for station_name in (
        "RadioTEDU Energize",
        "RadioTEDU Rock",
        "RadioTEDU Lo-Fi",
        "RadioTEDU Jazz",
        "RadioTEDU Classic",
        "RadioTEDU",
    ):
        rules = default_rules_for_station(station_name)
        assert len(rules) == 49
        assert len({rule.name for rule in rules}) == 49
        monday_bpm = [(rule.min_bpm, rule.max_bpm) for rule in rules if rule.day_of_week == 0]
        for day_of_week in range(1, 7):
            assert [(rule.min_bpm, rule.max_bpm) for rule in rules if rule.day_of_week == day_of_week] == monday_bpm


def test_daily_schema_migration_preserves_existing_rules_as_monday():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daypart_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER NOT NULL, position INTEGER NOT NULL, "
        "name TEXT NOT NULL, start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL, "
        "min_bpm REAL NOT NULL, max_bpm REAL NOT NULL, enabled INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(station_id, position))"
    )
    conn.execute(
        "INSERT INTO daypart_rules VALUES (1, 9, 0, 'Operator Monday', 300, 540, 90, 120, 1, 'now', 'now')"
    )
    _migrate_daypart_rules(conn.cursor())
    row = conn.execute("SELECT day_of_week, name FROM daypart_rules WHERE id=1").fetchone()
    assert row == (0, "Operator Monday")
    conn.close()


def test_worker_prefers_current_bpm_band_then_unknown_fallback(monkeypatch):
    init_db()
    conn = get_connection()
    station_id = _add_station(conn, "RadioTEDU Rock")
    conn.executemany(
        "INSERT INTO tracks(station_id, title, file_path, bpm, play_count) VALUES (?, ?, ?, ?, 0)",
        [
            (station_id, "Too Slow", "C:/music/slow.mp3", 70),
            (station_id, "In Range", "C:/music/right.mp3", 130),
            (station_id, "Unknown", "C:/music/unknown.mp3", 0),
        ],
    )
    conn.commit()
    rule = DaypartRule(0, "Test", 0, 720, 120, 140)
    monkeypatch.setattr(station_worker_module, "active_daypart", lambda _conn, _sid: rule)
    worker = StationWorker(station_id=station_id)
    selected = worker._select_random_music_track(set())
    title = conn.execute("SELECT title FROM tracks WHERE id=?", (selected["track_id"],)).fetchone()["title"]
    assert title == "In Range"
    conn.execute("UPDATE tracks SET is_active=0 WHERE title='In Range'")
    conn.commit()
    selected = worker._select_random_music_track(set())
    title = conn.execute("SELECT title FROM tracks WHERE id=?", (selected["track_id"],)).fetchone()["title"]
    assert title == "Unknown"
    worker.conn.close()
    conn.close()


def test_bpm_estimator_recognizes_120_bpm_click_track():
    samples = array.array("h", [0]) * (SAMPLE_RATE * 20)
    beat_samples = SAMPLE_RATE // 2
    for start in range(0, len(samples), beat_samples):
        for offset in range(min(500, len(samples) - start)):
            samples[start + offset] = int(28000 * (1.0 - offset / 500.0))
    bpm, confidence = estimate_bpm_from_samples(samples)
    assert 112 <= bpm <= 126
    assert confidence >= 0.04


def test_daypart_api_returns_defaults_and_validates_full_day(client):
    init_db()
    conn = get_connection()
    station_id = _add_station(conn, "RadioTEDU Jazz")
    conn.close()
    response = client.get(f"/api/dayparts?station_id={station_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert len(payload["rules"]) == 49
    assert len(payload["days"]) == 7
    assert payload["rules"][5]["name"] == "Midnight Ballads"
    assert payload["days"][1]["rules"][0]["name"] == "Tuesday in Swing"

    saved = client.put(
        f"/api/dayparts/{station_id}",
        json={
            "enabled": False,
            "timezone": payload["timezone"],
            "rules": [
                {
                    "day": rule["day"],
                    "name": "My Tuesday Show" if rule["day"] == "Tuesday" and rule["position"] == 0 else rule["name"],
                    "start": rule["start"],
                    "end": rule["end"],
                    "min_bpm": 101 if rule["day"] == "Tuesday" and rule["position"] == 0 else rule["min_bpm"],
                    "max_bpm": rule["max_bpm"],
                    "enabled": True,
                }
                for rule in payload["rules"]
            ],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is False
    assert saved.json()["days"][1]["rules"][0]["name"] == "My Tuesday Show"
    assert saved.json()["days"][1]["rules"][0]["min_bpm"] == 101

    invalid = client.put(
        f"/api/dayparts/{station_id}",
        json={
            "enabled": True,
            "timezone": "Europe/Istanbul",
            "rules": [{"name": "Gap", "start": "05:00", "end": "09:00", "min_bpm": 80, "max_bpm": 120}],
        },
    )
    assert invalid.status_code == 422


def test_bpm_api_stores_measured_value_instead_of_placeholder(client, monkeypatch):
    init_db()
    conn = get_connection()
    station_id = _add_station(conn, "RadioTEDU")
    cursor = conn.execute(
        "INSERT INTO tracks(station_id, title, file_path, bpm) VALUES (?, 'Measured', 'C:/music/measured.mp3', 0)",
        (station_id,),
    )
    track_id = int(cursor.lastrowid)
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", lambda _path: {"bpm": 0.0})
    monkeypatch.setattr("app.api.legacy.analyze_bpm", lambda _path: (137.2, 0.9))
    response = client.post(
        "/api/library/bpm/analyze",
        json={"station_id": station_id, "only_missing": True, "track_type": "music", "limit": 1},
    )
    assert response.status_code == 200
    assert response.json()["summary"]["bpm_updated"] == 1
    conn = get_connection()
    assert conn.execute("SELECT bpm FROM tracks WHERE id=?", (track_id,)).fetchone()["bpm"] == 137.2
    conn.close()


def test_primary_onair_contains_full_weekly_editor_controls():
    root = Path(__file__).resolve().parents[2]
    html = (root / "app/static/onair/index.html").read_text(encoding="utf-8")
    javascript = (root / "app/static/onair/app.js").read_text(encoding="utf-8")
    assert 'id="daypartTimezone"' in html
    assert 'id="daypartForm"' in html
    assert "function daypartRulesFromForm()" in javascript
    assert "function addDaypartRule(day)" in javascript
    assert "function removeDaypartRule(row)" in javascript
    assert "async function saveDayparts(event)" in javascript
