"""Tests for capability-based show lifecycle enforcement."""

from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    monkeypatch.setattr("app.main._autostart_station_worker_loops", lambda conn: None)

    from app.main import app
    from app.db import get_connection, init_db
    from app.repositories.station_repo import StationRepository
    from app.repositories.user_repo import UserRepository

    init_db()
    conn = get_connection()
    try:
        StationRepository(conn).create("Test FM")
        UserRepository(conn).create_user("dj1", "DJ One", "x", "dj")
        UserRepository(conn).create_user("prod1", "Producer One", "x", "producer")
        UserRepository(conn).create_user("viewer1", "Viewer One", "x", "viewer")
    finally:
        conn.close()
    return app


def _auth_headers(app, username="admin"):
    from app.auth.jwt_handler import create_access_token
    from app.db import get_connection
    from app.repositories.user_repo import UserRepository

    conn = get_connection()
    try:
        user = UserRepository(conn).get_user_by_username(username)
        token = create_access_token(user_id=int(user["id"]), role=str(user["role"]))
        return {"Authorization": f"Bearer {token}"}
    finally:
        conn.close()


def _create_show(app, station_id=1, name="Capability Show"):
    from app.db import get_connection
    from app.repositories.show_repo import ShowRepository

    conn = get_connection()
    try:
        return ShowRepository(conn).create(station_id, name)
    finally:
        conn.close()


def _create_station(app, name="Extra Station"):
    from app.db import get_connection
    from app.repositories.station_repo import StationRepository

    conn = get_connection()
    try:
        return StationRepository(conn).create(name)
    finally:
        conn.close()


def _assign_show_permissions(app, show_id: int, username: str, role: str, permission_keys: set[str]):
    from app.db import get_connection
    from app.repositories.rbac_repo import RbacRepository
    from app.repositories.show_repo import ShowRepository
    from app.repositories.user_repo import UserRepository

    conn = get_connection()
    try:
        user = UserRepository(conn).get_user_by_username(username)
        user_id = int(user["id"])
        ShowRepository(conn).assign(
            show_id,
            user_id,
            role=role,
            permission_keys=set(permission_keys),
        )
        RbacRepository(conn).replace_show_permissions(show_id, user_id, set(permission_keys))
        return user_id
    finally:
        conn.close()


def _assign_global_permissions(app, username: str, permission_keys: set[str]):
    from app.db import get_connection
    from app.repositories.rbac_repo import RbacRepository
    from app.repositories.user_repo import UserRepository

    conn = get_connection()
    try:
        user = UserRepository(conn).get_user_by_username(username)
        user_id = int(user["id"])
        rbac = RbacRepository(conn)
        role_id = rbac.create_role_template(f"{username}-global-role", "", False)
        rbac.replace_role_permissions(role_id, set(permission_keys))
        rbac.replace_user_roles(user_id, {role_id})
        return user_id
    finally:
        conn.close()


def _claim_workspace(client, headers: dict[str, str], station_id: int, show_id: int):
    return client.post(
        "/api/program/workspace/claim",
        headers=headers,
        json={"station_id": int(station_id), "show_id": int(show_id)},
    )


def _ensure_track(station_id=1):
    from app.db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tracks WHERE station_id = ? LIMIT 1", (station_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO tracks (station_id, title, artist, file_path, duration, is_active, track_type) "
                "VALUES (?, 'Test Track', 'Test Artist', 'test.mp3', 180.0, 1, 'music')",
                (station_id,),
            )
            conn.commit()
    finally:
        conn.close()


def _add_queue_tracks(station_id=1, count=5):
    from app.db import get_connection
    from app.repositories.program_queue_repo import ProgramQueueRepository

    _ensure_track(station_id)
    conn = get_connection()
    try:
        queue = ProgramQueueRepository(conn)
        for _ in range(count):
            queue.add_item(station_id, 1)
    finally:
        conn.close()


def _set_active_session_status(station_id: int, status: str):
    from app.db import get_connection
    from app.repositories.show_session_repo import ShowSessionRepository

    conn = get_connection()
    try:
        session = ShowSessionRepository(conn).get_active_for_station(station_id)
        assert session is not None
        ShowSessionRepository(conn).update_status(session["id"], status)
    finally:
        conn.close()


def test_producer_with_show_broadcast_permission_can_go_live(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="Producer Live Show")
        _assign_show_permissions(app, show_id, "prod1", "producer", {"show.broadcast"})
        headers = _auth_headers(app, "prod1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        _add_queue_tracks(1, count=5)
        response = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )

        assert response.status_code == 200
        assert response.json()["session"]["show_id"] == show_id


def test_user_without_show_break_control_cannot_go_break(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="No Break Control Show")
        _assign_show_permissions(app, show_id, "prod1", "producer", {"show.broadcast"})
        headers = _auth_headers(app, "prod1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        _add_queue_tracks(1, count=5)
        go_live = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )
        assert go_live.status_code == 200

        _set_active_session_status(1, "live")
        response = client.post(f"/api/shows/{show_id}/go-break", headers=headers)

        assert response.status_code == 403


def test_producer_with_show_break_control_can_go_break(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="Break Control Show")
        _assign_show_permissions(
            app,
            show_id,
            "prod1",
            "producer",
            {"show.broadcast", "show.break_control"},
        )
        headers = _auth_headers(app, "prod1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        _add_queue_tracks(1, count=5)
        go_live = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )
        assert go_live.status_code == 200

        _set_active_session_status(1, "live")
        response = client.post(f"/api/shows/{show_id}/go-break", headers=headers)

        assert response.status_code == 200
        assert response.json()["session"]["status"] == "break_outro"


def test_user_without_show_end_permission_cannot_end_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="No End Permission Show")
        _assign_show_permissions(app, show_id, "dj1", "dj", {"show.broadcast"})
        headers = _auth_headers(app, "dj1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        _add_queue_tracks(1, count=5)
        go_live = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )
        assert go_live.status_code == 200

        _set_active_session_status(1, "live")
        response = client.post(f"/api/shows/{show_id}/end", headers=headers)

        assert response.status_code == 403


def test_producer_with_show_end_permission_can_end_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="End Permission Show")
        _assign_show_permissions(
            app,
            show_id,
            "prod1",
            "producer",
            {"show.broadcast", "show.end"},
        )
        headers = _auth_headers(app, "prod1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        _add_queue_tracks(1, count=5)
        go_live = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )
        assert go_live.status_code == 200

        _set_active_session_status(1, "live")
        response = client.post(f"/api/shows/{show_id}/end", headers=headers)

        assert response.status_code == 200
        assert response.json()["session"]["status"] == "outro_playing"


def test_viewer_with_program_panel_and_show_assignment_can_list_assigned_shows(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="Viewer Assigned Show")
        _assign_show_permissions(app, show_id, "viewer1", "producer", {"show.queue_edit"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})

        headers = _auth_headers(app, "viewer1")
        response = client.get("/api/shows/?station_id=1", headers=headers)

        assert response.status_code == 200
        assert [show["id"] for show in response.json()] == [show_id]


def test_viewer_with_show_queue_edit_can_add_to_program_queue_before_live(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="Queue Edit Show")
        _assign_show_permissions(app, show_id, "viewer1", "producer", {"show.queue_edit"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})
        _ensure_track(1)

        headers = _auth_headers(app, "viewer1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        response = client.post(
            "/api/program/queue/items",
            headers=headers,
            json={"station_id": 1, "show_id": show_id, "track_id": 1},
        )

        assert response.status_code == 200
        assert response.json()["queue"]["items"][0]["track_id"] == 1


def test_user_without_show_queue_edit_cannot_add_to_program_queue(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="No Queue Edit Show")
        _assign_show_permissions(app, show_id, "prod1", "producer", {"show.broadcast"})
        _assign_global_permissions(app, "prod1", {"program.panel.open"})
        _ensure_track(1)

        headers = _auth_headers(app, "prod1")
        response = client.post(
            "/api/program/queue/items",
            headers=headers,
            json={"station_id": 1, "show_id": show_id, "track_id": 1},
        )

        assert response.status_code == 403


def test_viewer_with_show_jingle_manage_can_upload_show_audio(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, name="Jingle Manage Show")
        _assign_show_permissions(app, show_id, "viewer1", "producer", {"show.jingle_manage"})

        headers = _auth_headers(app, "viewer1")
        response = client.post(
            f"/api/shows/{show_id}/upload-audio",
            headers=headers,
            data={"type": "intro"},
            files={"file": ("intro.mp3", b"1234", "audio/mpeg")},
        )

        assert response.status_code == 200
        assert response.json()["type"] == "intro"


def test_station_assigned_user_cannot_read_other_station_program_state(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        station_one_show_id = _create_show(app, station_id=1, name="Station One Show")
        station_two_id = _create_station(app, name="Station Two")
        _create_show(app, station_id=station_two_id, name="Station Two Show")
        _assign_show_permissions(app, station_one_show_id, "viewer1", "producer", {"show.queue_edit"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})

        headers = _auth_headers(app, "viewer1")
        queue_response = client.get(f"/api/program/queue?station_id={station_two_id}", headers=headers)
        session_response = client.get(f"/api/shows/session/current?station_id={station_two_id}", headers=headers)

        assert queue_response.status_code == 403
        assert session_response.status_code == 403


def test_same_station_user_cannot_read_different_show_prelive_state(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_a_id = _create_show(app, station_id=1, name="Assigned Show A")
        show_b_id = _create_show(app, station_id=1, name="Unassigned Show B")
        _assign_show_permissions(app, show_a_id, "viewer1", "producer", {"show.queue_edit"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})

        headers = _auth_headers(app, "viewer1")
        queue_response = client.get(
            f"/api/program/queue?station_id=1&show_id={show_b_id}",
            headers=headers,
        )
        session_response = client.get(
            f"/api/shows/session/current?station_id=1&show_id={show_b_id}",
            headers=headers,
        )

        assert queue_response.status_code == 403
        assert session_response.status_code == 403


def test_prelive_program_queue_requires_explicit_show_selection(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, station_id=1, name="Selected Show Required")
        _assign_show_permissions(app, show_id, "viewer1", "viewer", {"show.queue_edit"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})

        headers = _auth_headers(app, "viewer1")
        response = client.get("/api/program/queue?station_id=1", headers=headers)

        assert response.status_code == 400
        assert "show_id" in str(response.json().get("detail") or "")


def test_show_broadcast_user_can_switch_program_music_mode(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, station_id=1, name="Broadcast Mode Show")
        _assign_show_permissions(app, show_id, "viewer1", "viewer", {"show.broadcast"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})

        headers = _auth_headers(app, "viewer1")
        claimed = _claim_workspace(client, headers, 1, show_id)
        assert claimed.status_code == 200, claimed.text
        response = client.post(
            "/api/liquidsoap/program/music",
            params={"station_id": 1, "show_id": show_id, "mode": "duck"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["effective_mode"] == "duck"


def test_show_broadcast_user_cannot_go_live_without_claimed_workspace(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, station_id=1, name="Claim Required Live Show")
        _assign_show_permissions(app, show_id, "viewer1", "viewer", {"show.broadcast"})
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})
        _add_queue_tracks(1, count=5)

        headers = _auth_headers(app, "viewer1")
        response = client.post(
            f"/api/shows/{show_id}/go-live",
            headers=headers,
            json={"station_id": 1},
        )

        assert response.status_code == 409
        assert "workspace" in str(response.json().get("detail") or "").lower()


def test_prelive_program_workspace_claim_blocks_other_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_a_id = _create_show(app, station_id=1, name="Prelive Show A")
        show_b_id = _create_show(app, station_id=1, name="Prelive Show B")
        _assign_show_permissions(
            app,
            show_a_id,
            "viewer1",
            "viewer",
            {"show.broadcast", "show.queue_edit"},
        )
        _assign_show_permissions(
            app,
            show_b_id,
            "prod1",
            "producer",
            {"show.broadcast", "show.queue_edit"},
        )
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})
        _assign_global_permissions(app, "prod1", {"program.panel.open"})
        _ensure_track(station_id=1)

        viewer_headers = _auth_headers(app, "viewer1")
        producer_headers = _auth_headers(app, "prod1")

        claimed = _claim_workspace(client, viewer_headers, 1, show_a_id)
        assert claimed.status_code == 200, claimed.text

        add_for_owner = client.post(
            "/api/program/queue/items",
            headers=viewer_headers,
            json={"station_id": 1, "show_id": show_a_id, "track_id": 1},
        )
        assert add_for_owner.status_code == 200, add_for_owner.text

        blocked = client.get(
            f"/api/program/queue?station_id=1&show_id={show_b_id}",
            headers=producer_headers,
        )

        assert blocked.status_code == 409


def test_program_workspace_release_clears_claim_and_allows_next_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_a_id = _create_show(app, station_id=1, name="Release Show A")
        show_b_id = _create_show(app, station_id=1, name="Release Show B")
        _assign_show_permissions(
            app,
            show_a_id,
            "viewer1",
            "viewer",
            {"show.broadcast", "show.queue_edit"},
        )
        _assign_show_permissions(
            app,
            show_b_id,
            "prod1",
            "producer",
            {"show.broadcast", "show.queue_edit"},
        )
        _assign_global_permissions(app, "viewer1", {"program.panel.open"})
        _assign_global_permissions(app, "prod1", {"program.panel.open"})
        _ensure_track(station_id=1)

        viewer_headers = _auth_headers(app, "viewer1")
        producer_headers = _auth_headers(app, "prod1")

        claimed = _claim_workspace(client, viewer_headers, 1, show_a_id)
        assert claimed.status_code == 200, claimed.text

        add_for_owner = client.post(
            "/api/program/queue/items",
            headers=viewer_headers,
            json={"station_id": 1, "show_id": show_a_id, "track_id": 1},
        )
        assert add_for_owner.status_code == 200, add_for_owner.text

        released = client.delete(
            f"/api/program/workspace/claim?station_id=1&show_id={show_a_id}",
            headers=viewer_headers,
        )
        assert released.status_code == 200, released.text
        assert released.json()["queue"]["items"] == []

        claimed_next = _claim_workspace(client, producer_headers, 1, show_b_id)
        assert claimed_next.status_code == 200, claimed_next.text


def test_liquidsoap_status_requires_show_context_before_live(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show(app, station_id=1, name="Status Context Show")
        _assign_show_permissions(app, show_id, "prod1", "producer", {"show.broadcast"})
        _assign_global_permissions(app, "prod1", {"program.panel.open"})

        headers = _auth_headers(app, "prod1")
        response = client.get("/api/liquidsoap/status?station_id=1", headers=headers)

        assert response.status_code == 400
        assert "show_id" in str(response.json().get("detail") or "")
