from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def _seed_track(conn, track_id: int, *, track_type: str, station_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO tracks (id, station_id, title, artist, file_path, track_type, is_active, duration) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 30)",
        (
            track_id,
            station_id,
            f"Planner {track_type} {track_id}",
            "Planner Test Artist",
            f"C:/music/planner-{track_id}.mp3",
            track_type,
        ),
    )


def _payload(*, plan_type: str, track_id: int, tomorrow: date) -> dict:
    return {
        "name": f"Test {plan_type} plan",
        "plan_type": plan_type,
        "source_station_id": 1,
        "track_id": track_id,
        "station_ids": [1, 2],
        "starts_on": tomorrow.isoformat(),
        "ends_on": tomorrow.isoformat(),
        "weekdays": [tomorrow.isoweekday()],
        "local_start": "09:00",
        "local_end": "11:00",
        "timezone": "Europe/Istanbul",
        "repeat_every_minutes": 0,
        "sweeper_every_songs": 2,
        "play_window_minutes": 15,
        "priority": 10,
        "enabled": True,
    }


def test_broadcast_plan_targets_multiple_stations_and_reads_back_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (1, 'Main')")
    conn.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (2, 'Secondary')")
    _seed_track(conn, 99001, track_type="ad")
    conn.commit()
    conn.close()
    client = TestClient(app)
    payload = _payload(
        plan_type="ad",
        track_id=99001,
        tomorrow=date.today() + timedelta(days=1),
    )

    created = client.post("/api/broadcast-plans", json=payload)
    assert created.status_code == 200, created.text
    plan = created.json()["plan"]
    assert plan["plan_type"] == "ad"
    assert plan["local_start"] == "09:00"
    assert plan["local_end"] == "11:00"
    assert plan["timezone"] == "Europe/Istanbul"
    assert [int(target["station_id"]) for target in plan["targets"]] == [1, 2]
    assert all(target["track_title"] == "Planner ad 99001" for target in plan["targets"])

    listed = client.get("/api/broadcast-plans", params={"station_id": 2})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["plans"]] == [plan["id"]]

    disabled = client.post(
        f"/api/broadcast-plans/{plan['id']}/enabled",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["plan"]["enabled"] is False


def test_broadcast_planner_rejects_wrong_media_type_and_repeating_sweepers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (1, 'Main')")
    conn.execute("INSERT OR IGNORE INTO stations (id, name) VALUES (2, 'Secondary')")
    _seed_track(conn, 99002, track_type="music")
    _seed_track(conn, 99003, track_type="jingle")
    conn.commit()
    conn.close()
    client = TestClient(app)
    tomorrow = date.today() + timedelta(days=1)

    wrong_type = client.post(
        "/api/broadcast-plans",
        json=_payload(plan_type="ad", track_id=99002, tomorrow=tomorrow),
    )
    assert wrong_type.status_code == 409
    assert wrong_type.json()["detail"] == "track_type_must_match_ad"

    repeating_sweeper = _payload(
        plan_type="sweeper",
        track_id=99003,
        tomorrow=tomorrow,
    )
    repeating_sweeper["repeat_every_minutes"] = 30
    response = client.post("/api/broadcast-plans", json=repeating_sweeper)
    assert response.status_code == 422
    assert response.json()["detail"] == "sweeper_uses_song_interval"
