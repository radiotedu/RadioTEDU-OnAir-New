from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.schedule_repo import ScheduleRepository


def test_queue_push_and_state_show_manual_source():
    client = TestClient(app)
    push_res = client.post("/api/queue/push", json={"station_id": 1, "track_id": 77})
    assert push_res.status_code == 200
    state_res = client.get("/api/playout/state", params={"station_id": 1})
    assert state_res.status_code == 200
    assert state_res.json()["next_source"] == "manual"


def test_queue_items_endpoint_lists_recent_items():
    client = TestClient(app)
    push_res = client.post("/api/queue/push", json={"station_id": 2, "track_id": 88})
    assert push_res.status_code == 200

    res = client.get("/api/queue/items", params={"station_id": 2})
    assert res.status_code == 200
    payload = res.json()
    assert payload["station_id"] == 2
    assert isinstance(payload["items"], list)
    assert any(int(item["track_id"]) == 88 for item in payload["items"])


def test_playout_state_prefers_ads_when_no_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    AdBreakRepository(conn).enqueue(
        station_id=3,
        track_id=301,
        due_at="2000-01-01 00:00:00",
        priority=5,
    )
    ScheduleRepository(conn).enqueue(
        station_id=3,
        track_id=302,
        play_at="2000-01-01 00:00:00",
    )

    client = TestClient(app)
    res = client.get("/api/playout/state", params={"station_id": 3})
    assert res.status_code == 200
    assert res.json()["next_source"] == "ads"


def test_playout_state_uses_schedule_when_ads_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    ScheduleRepository(conn).enqueue(
        station_id=4,
        track_id=401,
        play_at="2000-01-01 00:00:00",
    )

    client = TestClient(app)
    res = client.get("/api/playout/state", params={"station_id": 4})
    assert res.status_code == 200
    assert res.json()["next_source"] == "schedule"
