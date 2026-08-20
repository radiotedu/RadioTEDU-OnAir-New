"""Tests for show permission admin APIs."""

from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    from app.main import app
    from app.db import get_connection, init_db
    from app.repositories.station_repo import StationRepository
    from app.repositories.user_repo import UserRepository

    init_db()
    conn = get_connection()
    StationRepository(conn).create("Test FM")
    UserRepository(conn).create_user("dj1", "DJ One", "x", "dj")
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
    finally:
        conn.close()
    return {"Authorization": f"Bearer {token}"}


def test_assign_user_serializes_permission_keys(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    response = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "producer",
            "permission_keys": ["show.end", "show.broadcast"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assignment = response.json()["assignments"][0]
    assert assignment["user_id"] == 2
    assert assignment["role"] == "producer"
    assert assignment["permission_keys"] == sorted(assignment["permission_keys"])
    assert {"show.broadcast", "show.end"}.issubset(set(assignment["permission_keys"]))

    listed = client.get(f"/api/shows/{show_id}/assignments", headers=headers)
    assert listed.status_code == 200
    assert {"show.broadcast", "show.end"}.issubset(
        set(listed.json()[0]["permission_keys"])
    )


def test_assign_user_replaces_permission_keys(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    first = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "dj",
            "permission_keys": ["show.broadcast", "show.end"],
        },
        headers=headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "producer",
            "permission_keys": [],
        },
        headers=headers,
    )
    assert second.status_code == 200
    assignment = second.json()["assignments"][0]
    assert assignment["role"] == "producer"
    assert assignment["permission_keys"] == []

    listed = client.get(f"/api/shows/{show_id}/assignments", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["permission_keys"] == []

    from app.db import get_connection
    from app.repositories.rbac_repo import RbacRepository

    conn = get_connection()
    try:
        permission_keys = RbacRepository(conn).list_show_permissions(show_id, 2)
    finally:
        conn.close()

    assert permission_keys == set()


def test_assign_user_rejects_invalid_permission_keys(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    response = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "dj",
            "permission_keys": ["show.broadcast", "not.a.permission"],
        },
        headers=headers,
    )

    assert response.status_code == 400


def test_explicit_show_permissions_survive_later_init_db_cycle(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    assigned = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "producer",
            "permission_keys": ["show.broadcast", "show.end"],
        },
        headers=headers,
    )
    assert assigned.status_code == 200

    from app.db import init_db

    init_db()

    listed = client.get(f"/api/shows/{show_id}/assignments", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["permission_keys"] == ["show.broadcast", "show.end"]

    reassigned = client.post(
        f"/api/shows/{show_id}/assign",
        json={"user_id": 2, "role": "producer", "permission_keys": []},
        headers=headers,
    )
    assert reassigned.status_code == 200

    init_db()

    cleared = client.get(f"/api/shows/{show_id}/assignments", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()[0]["permission_keys"] == []


def test_assign_without_permission_keys_preserves_existing_explicit_set(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    first = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "dj",
            "permission_keys": ["show.broadcast", "show.end"],
        },
        headers=headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/shows/{show_id}/assign",
        json={"user_id": 2, "role": "producer"},
        headers=headers,
    )
    assert second.status_code == 200
    assignment = second.json()["assignments"][0]
    assert assignment["permission_keys"] == ["show.broadcast", "show.end"]

    listed = client.get(f"/api/shows/{show_id}/assignments", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["permission_keys"] == ["show.broadcast", "show.end"]


def test_public_assignment_payload_does_not_expose_storage_column(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")

    created = client.post(
        "/api/shows/",
        json={"station_id": 1, "name": "Morning Show"},
        headers=headers,
    )
    show_id = int(created.json()["id"])

    response = client.post(
        f"/api/shows/{show_id}/assign",
        json={
            "user_id": 2,
            "role": "dj",
            "permission_keys": ["show.broadcast"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assignment = response.json()["assignments"][0]
    assert "permission_keys_json" not in assignment
