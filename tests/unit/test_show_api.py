"""Tests for shows API endpoints."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))

    from app.main import app
    from app.db import init_db, get_connection
    from app.repositories.user_repo import UserRepository
    from app.repositories.station_repo import StationRepository

    init_db()
    conn = get_connection()
    StationRepository(conn).create("Test FM")
    # admin is auto-created by init_db(); add a DJ for assignment tests
    UserRepository(conn).create_user("dj1", "DJ One", "x", "dj")
    conn.close()
    return app


def _auth_headers(app, username="admin"):
    """Generate Bearer headers. For admin, use conftest auto-auth (pass None).
    For non-admin users, mint a token directly."""
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


def test_create_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    resp = client.post("/api/shows/", json={
        "station_id": 1, "name": "Morning Show", "description": "Good morning!"
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Morning Show"
    assert data["id"] >= 1


def test_list_shows(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    client.post("/api/shows/", json={"station_id": 1, "name": "Show A"}, headers=headers)
    client.post("/api/shows/", json={"station_id": 1, "name": "Show B"}, headers=headers)
    resp = client.get("/api/shows/?station_id=1", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_dj_sees_only_assigned_shows(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    admin_h = _auth_headers(app, "admin")
    dj_h = _auth_headers(app, "dj1")
    r1 = client.post("/api/shows/", json={"station_id": 1, "name": "Assigned"}, headers=admin_h)
    client.post("/api/shows/", json={"station_id": 1, "name": "Not Assigned"}, headers=admin_h)
    show_id = r1.json()["id"]
    # Assign dj1 (user_id=2) to first show
    client.post(f"/api/shows/{show_id}/assign", json={"user_id": 2, "role": "dj"}, headers=admin_h)
    resp = client.get("/api/shows/?station_id=1", headers=dj_h)
    assert resp.status_code == 200
    shows = resp.json()
    assert len(shows) == 1
    assert shows[0]["name"] == "Assigned"


def test_get_single_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Solo Show"}, headers=headers)
    show_id = r.json()["id"]
    resp = client.get(f"/api/shows/{show_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Solo Show"


def test_dj_cannot_fetch_unassigned_show_by_id(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    admin_h = _auth_headers(app, "admin")
    dj_h = _auth_headers(app, "dj1")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Private Show"}, headers=admin_h)
    show_id = r.json()["id"]
    resp = client.get(f"/api/shows/{show_id}", headers=dj_h)
    assert resp.status_code == 403


def test_get_nonexistent_show_404(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    resp = client.get("/api/shows/9999", headers=headers)
    assert resp.status_code == 404


def test_update_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Old"}, headers=headers)
    show_id = r.json()["id"]
    resp = client.put(f"/api/shows/{show_id}", json={"name": "New"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


def test_delete_show(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Doomed"}, headers=headers)
    show_id = r.json()["id"]
    resp = client.delete(f"/api/shows/{show_id}", headers=headers)
    assert resp.status_code == 200
    resp2 = client.get(f"/api/shows/{show_id}", headers=headers)
    assert resp2.status_code == 404


def test_dj_cannot_create_show_403(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    dj_h = _auth_headers(app, "dj1")
    resp = client.post("/api/shows/", json={"station_id": 1, "name": "Nope"}, headers=dj_h)
    assert resp.status_code == 403


def test_assign_and_unassign(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Show"}, headers=headers)
    show_id = r.json()["id"]
    resp = client.post(f"/api/shows/{show_id}/assign", json={"user_id": 2, "role": "dj"}, headers=headers)
    assert resp.status_code == 200
    resp = client.delete(f"/api/shows/{show_id}/assign/2", headers=headers)
    assert resp.status_code == 200


import io


def test_upload_show_audio(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    headers = _auth_headers(app, "admin")
    r = client.post("/api/shows/", json={"station_id": 1, "name": "Show"}, headers=headers)
    show_id = r.json()["id"]
    fake_audio = io.BytesIO(b"\x00" * 1024)
    resp = client.post(
        f"/api/shows/{show_id}/upload-audio",
        headers=headers,
        data={"type": "intro"},
        files={"file": ("intro.mp3", fake_audio, "audio/mpeg")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "intro"
    assert data["file_path"] is not None
    # Verify show was updated
    show = client.get(f"/api/shows/{show_id}", headers=headers).json()
    assert show["intro_path"] is not None
