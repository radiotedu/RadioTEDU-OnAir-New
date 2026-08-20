from fastapi.testclient import TestClient

from app.api import runtime as runtime_api
from app.db import get_connection, init_db
from app.engine.playout_state import PlayoutStateService
from app.main import app


def _seed_interrupted_program() -> tuple[int, int, int]:
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        track_ids = []
        for title, track_type in (
            ("Current song", "music"),
            ("Next song", "music"),
            ("Station jingle", "jingle"),
        ):
            cur.execute(
                "INSERT INTO tracks "
                "(station_id, title, artist, track_type, duration, file_path, is_active) "
                "VALUES (1, ?, 'RadioTEDU', ?, 180, ?, 1)",
                (title, track_type, f"C:/audio/{title}.mp3"),
            )
            track_ids.append(int(cur.lastrowid))
        cur.execute(
            "INSERT INTO queue_items "
            "(station_id, track_id, position, status, started_at) "
            "VALUES (1, ?, 1, 'playing', CURRENT_TIMESTAMP)",
            (track_ids[0],),
        )
        queue_item_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO queue_items "
            "(station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')",
            (track_ids[1],),
        )
        cur.execute(
            "INSERT INTO ad_break_items "
            "(station_id, track_id, due_at, status, started_at) "
            "VALUES (1, ?, CURRENT_TIMESTAMP, 'playing', CURRENT_TIMESTAMP)",
            (track_ids[2],),
        )
        ad_item_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO schedule_items "
            "(station_id, track_id, play_at, status) "
            "VALUES (1, ?, CURRENT_TIMESTAMP, 'playing')",
            (track_ids[2],),
        )
        schedule_item_id = int(cur.lastrowid)
        conn.commit()
        PlayoutStateService(conn).set_current(
            1,
            "manual",
            queue_item_id,
            reason="test_interrupted_program",
        )
        return queue_item_id, ad_item_id, schedule_item_id
    finally:
        conn.close()


def test_operator_stop_preserves_queue_order_and_requeues_interrupted_items():
    queue_item_id, ad_item_id, schedule_item_id = _seed_interrupted_program()

    result = runtime_api._preserve_operator_playout(1)

    assert result["playlist_preserved"] is True
    assert result["queue_items_before"] == 2
    assert result["queue_items_after"] == 2
    assert result["queue_items_requeued"] == 1
    assert result["ads_requeued"] == 1
    assert result["schedules_requeued"] == 1
    assert "restarts from its beginning" in result["resume_behavior"]

    conn = get_connection()
    try:
        queue_rows = conn.execute(
            "SELECT id, position, status, started_at FROM queue_items "
            "WHERE station_id=1 ORDER BY position"
        ).fetchall()
        assert [(int(row["position"]), row["status"]) for row in queue_rows] == [
            (1, "pending"),
            (2, "pending"),
        ]
        assert int(queue_rows[0]["id"]) == queue_item_id
        assert queue_rows[0]["started_at"] is None
        assert conn.execute(
            "SELECT status FROM ad_break_items WHERE id=?", (ad_item_id,)
        ).fetchone()["status"] == "pending"
        assert conn.execute(
            "SELECT status FROM schedule_items WHERE id=?", (schedule_item_id,)
        ).fetchone()["status"] == "pending"
        assert PlayoutStateService(conn).get_current(1) == {
            "source": "none",
            "item_id": None,
        }
    finally:
        conn.close()


def test_operator_stop_endpoint_stops_outputs_and_reports_preservation(monkeypatch):
    calls = []

    monkeypatch.setattr(
        runtime_api.worker_loop_manager,
        "stop",
        lambda station_id: {
            "station_id": station_id,
            "running": False,
            "ticks": 4,
        },
    )
    monkeypatch.setattr(
        runtime_api.worker_loop_manager,
        "status",
        lambda station_id: {"station_id": station_id, "running": False},
    )
    monkeypatch.setattr(
        runtime_api.runtime_registry,
        "status",
        lambda station_id: {
            "station_id": station_id,
            "running": False,
            "program_running": bool(not calls),
        },
    )
    monkeypatch.setattr(
        runtime_api.runtime_registry,
        "stop_station",
        lambda station_id: calls.append(("stop", int(station_id))),
    )
    monkeypatch.setattr(runtime_api, "_broadcast_runtime_events", lambda *args, **kwargs: None)

    with TestClient(app) as client:
        _seed_interrupted_program()
        response = client.post("/api/runtime/1/operator-stop")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert calls == [("stop", 1)]
    assert payload["playlist_preserved"] is True
    assert payload["queue_items_before"] == payload["queue_items_after"] == 2
    assert payload["runtime_was_running"] is True


def test_ai_readiness_never_vetoes_operator_authorized_autostart(monkeypatch):
    init_db()
    conn = get_connection()
    try:
        from app.main import _autostart_station_worker_loops
        from app.repositories.settings_repo import SettingsRepository

        SettingsRepository(conn).upsert_station(
            1,
            {
                "broadcast_autostart_enabled": "true",
                "ai_host_enabled": "true",
                "startup_ai_readiness_state": "warming",
            },
        )
        started = []
        monkeypatch.setattr(
            "app.api.runtime.worker_loop_manager.start",
            lambda **kwargs: started.append(dict(kwargs)),
        )

        _autostart_station_worker_loops(conn)

        assert [item["station_id"] for item in started] == [1]
    finally:
        conn.close()


def test_jingle_cadence_defaults_to_two_songs_and_accepts_operator_value_three():
    with TestClient(app) as client:
        initial = client.get("/api/sweeper/config", params={"station_id": 1})
        assert initial.status_code == 200, initial.text
        assert initial.json()["interval"] == 2
        assert initial.json()["interval_unit"] == "tracks"
        assert initial.json()["mode"] == "ordered"

        changed = client.post(
            "/api/sweeper/config",
            json={
                "station_id": 1,
                "enabled": False,
                "interval": 3,
                "interval_unit": "tracks",
                "mode": "random",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["interval"] == 3
        assert changed.json()["mode"] == "random"

        read_back = client.get("/api/sweeper/config", params={"station_id": 1})
        assert read_back.status_code == 200, read_back.text
        assert read_back.json()["interval"] == 3
        assert read_back.json()["interval_unit"] == "tracks"
        assert read_back.json()["mode"] == "random"
