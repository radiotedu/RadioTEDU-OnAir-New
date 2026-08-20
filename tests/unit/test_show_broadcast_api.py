# tests/unit/test_show_broadcast_api.py
"""Tests for show broadcast API endpoints (go-live, go-break, end, current-session)."""
import pytest
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    monkeypatch.setattr("app.main._autostart_station_worker_loops", lambda conn: None)

    from app.main import app
    from app.db import init_db, get_connection
    from app.repositories.station_repo import StationRepository
    from app.repositories.user_repo import UserRepository

    init_db()
    conn = get_connection()
    StationRepository(conn).create("Test FM")
    UserRepository(conn).create_user("dj1", "DJ One", "x", "dj")
    UserRepository(conn).create_user("prod1", "Producer One", "x", "producer")
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


def _create_show_and_assign(app, station_id=1, dj_username="dj1"):
    """Create a show and assign the DJ to it. Returns show_id."""
    from app.db import get_connection
    from app.repositories.show_repo import ShowRepository
    from app.repositories.user_repo import UserRepository

    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        show_id = repo.create(station_id, "Test Show")
        user = UserRepository(conn).get_user_by_username(dj_username)
        repo.assign(show_id, int(user["id"]), role="dj")
        return show_id
    finally:
        conn.close()


def _ensure_track(station_id=1):
    """Ensure at least one track exists in the DB for queue items."""
    from app.db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tracks WHERE station_id = ? LIMIT 1", (station_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO tracks (station_id, title, artist, file_path, duration, is_active, track_type) "
                "VALUES (?, 'Test Track', 'Test', 'test.mp3', 180.0, 1, 'music')",
                (station_id,),
            )
            conn.commit()
    finally:
        conn.close()


def _add_queue_tracks(station_id, count=5):
    """Add pending tracks to the host queue so go-live validation passes."""
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


def _claim_workspace(
    client: TestClient,
    headers: dict[str, str],
    station_id: int,
    show_id: int,
    *,
    populate_queue: bool = True,
):
    response = client.post(
        "/api/program/workspace/claim",
        headers=headers,
        json={"station_id": int(station_id), "show_id": int(show_id)},
    )
    if response.status_code == 200 and populate_queue:
        _add_queue_tracks(station_id, count=5)
    return response


def test_go_live_creates_session(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        resp = client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["session"]["status"] == "going_live"
        assert data["session"]["show_id"] == show_id


def test_go_live_requires_dj_assignment(tmp_path, monkeypatch):
    """A DJ not assigned to the show cannot go live."""
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        # Create show but don't assign dj1
        from app.db import get_connection
        from app.repositories.show_repo import ShowRepository
        conn = get_connection()
        show_id = ShowRepository(conn).create(1, "Unassigned Show")
        conn.close()
        headers = _auth_headers(app, "dj1")
        resp = client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        assert resp.status_code == 403


def test_go_live_prevents_concurrent_session(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        resp1 = client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        assert resp1.status_code == 200
        resp2 = client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        assert resp2.status_code == 409


def test_go_live_requires_min_queue_tracks(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        # Don't add any tracks
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id, populate_queue=False)
        assert claim.status_code == 200, claim.text
        resp = client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        assert resp.status_code == 400
        assert "queue" in resp.json()["detail"].lower()


def test_go_break_transitions_to_break_outro(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        # Go live first
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        # Manually set session to 'live' for break test
        from app.db import get_connection
        from app.repositories.show_session_repo import ShowSessionRepository
        conn = get_connection()
        session = ShowSessionRepository(conn).get_active_for_station(1)
        ShowSessionRepository(conn).update_status(session["id"], "live")
        conn.close()
        resp = client.post(f"/api/shows/{show_id}/go-break", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "break_outro"


def test_go_break_requires_live_status(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        # Session is in going_live, not live — break should fail
        resp = client.post(f"/api/shows/{show_id}/go-break", headers=headers)
        assert resp.status_code == 400


def test_end_show_transitions_to_outro(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        from app.db import get_connection
        from app.repositories.show_session_repo import ShowSessionRepository
        conn = get_connection()
        session = ShowSessionRepository(conn).get_active_for_station(1)
        ShowSessionRepository(conn).update_status(session["id"], "live")
        conn.close()
        resp = client.post(f"/api/shows/{show_id}/end", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "outro_playing"


def test_end_show_dj_only(tmp_path, monkeypatch):
    """Producers cannot end shows — only DJs and admins."""
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        # Assign producer too
        from app.db import get_connection
        from app.repositories.show_repo import ShowRepository
        from app.repositories.user_repo import UserRepository
        conn = get_connection()
        prod = UserRepository(conn).get_user_by_username("prod1")
        ShowRepository(conn).assign(show_id, int(prod["id"]), role="producer")
        conn.close()
        dj_headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, dj_headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=dj_headers)
        from app.repositories.show_session_repo import ShowSessionRepository
        conn = get_connection()
        session = ShowSessionRepository(conn).get_active_for_station(1)
        ShowSessionRepository(conn).update_status(session["id"], "live")
        conn.close()
        prod_headers = _auth_headers(app, "prod1")
        resp = client.post(f"/api/shows/{show_id}/end", headers=prod_headers)
        assert resp.status_code == 403


def test_get_current_session(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=headers)
        resp = client.get("/api/shows/session/current?station_id=1", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["session"]["show_id"] == show_id


def test_get_current_session_returns_null_when_no_session(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/api/shows/session/current?station_id=1")
        assert resp.status_code == 200
        assert resp.json()["session"] is None


def test_go_break_allowed_for_producer(tmp_path, monkeypatch):
    """Producers assigned to the show can trigger ad breaks."""
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        show_id = _create_show_and_assign(app)
        _add_queue_tracks(1, count=5)
        from app.db import get_connection
        from app.repositories.show_repo import ShowRepository
        from app.repositories.user_repo import UserRepository
        conn = get_connection()
        prod = UserRepository(conn).get_user_by_username("prod1")
        ShowRepository(conn).assign(show_id, int(prod["id"]), role="producer")
        conn.close()
        dj_headers = _auth_headers(app, "dj1")
        claim = _claim_workspace(client, dj_headers, 1, show_id)
        assert claim.status_code == 200, claim.text
        client.post(f"/api/shows/{show_id}/go-live", json={"station_id": 1}, headers=dj_headers)
        from app.repositories.show_session_repo import ShowSessionRepository
        conn = get_connection()
        session = ShowSessionRepository(conn).get_active_for_station(1)
        ShowSessionRepository(conn).update_status(session["id"], "live")
        conn.close()
        prod_headers = _auth_headers(app, "prod1")
        resp = client.post(f"/api/shows/{show_id}/go-break", headers=prod_headers)
        assert resp.status_code == 200
