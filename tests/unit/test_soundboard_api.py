from pathlib import Path

import app.api.runtime as runtime_api
from app.ws.broadcaster import broadcaster as broadcaster_instance

from app.auth.password import hash_password
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.user_repo import UserRepository
from tests.conftest import login_and_get_headers


def _create_user_with_permissions(username: str, password: str, permission_keys: set[str]) -> None:
    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        rbac = RbacRepository(conn)
        existing = users.get_user_by_username(username)
        if existing is None:
            user_id = users.create_user(
                username=username,
                display_name=username.replace("-", " ").title(),
                password_hash=hash_password(password),
                role="viewer",
            )
        else:
            user_id = int(existing["id"])
            users.update_user(
                user_id,
                display_name=username.replace("-", " ").title(),
                password_hash=hash_password(password),
                role="viewer",
                is_active=1,
            )
        role_id = rbac.create_role_template(f"{username}-role", "", False)
        rbac.replace_role_permissions(role_id, set(permission_keys))
        rbac.replace_user_roles(user_id, {role_id})
    finally:
        conn.close()


def test_soundboard_requires_auth(client):
    response = client.get(
        "/api/soundboard/?station_id=1",
        headers={"X-Test-No-Auto-Auth": "1"},
    )
    assert response.status_code == 401


def test_soundboard_list_empty(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    response = client.get("/api/soundboard/?station_id=1", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_soundboard_create_requires_admin(client):
    from tests.conftest import _ensure_user, login_and_get_headers as _login
    _ensure_user("dj-user", "DJ User", "pass-1234", "dj")
    dj_headers = _login(client, "dj-user", "pass-1234")
    response = client.post(
        "/api/soundboard/",
        json={"station_id": 1, "name": "Test", "file_path": "/test.mp3"},
        headers=dj_headers,
    )
    assert response.status_code == 403


def test_soundboard_create_and_list(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    create_resp = client.post(
        "/api/soundboard/",
        json={"station_id": 1, "name": "Jingle", "file_path": "/sfx/jingle.mp3", "color": "#ff0000"},
        headers=admin_headers,
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["name"] == "Jingle"
    assert body["id"] > 0
    list_resp = client.get("/api/soundboard/?station_id=1", headers=admin_headers)
    items = list_resp.json()
    assert any(i["name"] == "Jingle" for i in items)


def test_soundboard_update(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    create_resp = client.post(
        "/api/soundboard/",
        json={"station_id": 1, "name": "Old", "file_path": "/old.mp3"},
        headers=admin_headers,
    )
    item_id = create_resp.json()["id"]
    update_resp = client.put(
        f"/api/soundboard/{item_id}",
        json={"name": "New"},
        headers=admin_headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "New"


def test_soundboard_delete(client):
    admin_headers = login_and_get_headers(client, "admin", "changeme")
    create_resp = client.post(
        "/api/soundboard/",
        json={"station_id": 1, "name": "Deleteme", "file_path": "/del.mp3"},
        headers=admin_headers,
    )
    item_id = create_resp.json()["id"]
    del_resp = client.delete(f"/api/soundboard/{item_id}", headers=admin_headers)
    assert del_resp.status_code == 204


def test_soundboard_forbidden_for_viewer(client):
    from tests.conftest import _ensure_user, login_and_get_headers as _login
    _ensure_user("viewer-user", "Viewer User", "pass-1234", "viewer")
    viewer_headers = _login(client, "viewer-user", "pass-1234")
    response = client.get("/api/soundboard/?station_id=1", headers=viewer_headers)
    assert response.status_code == 403


def test_soundboard_play_allows_permission_only_user(client, monkeypatch, tmp_path):
    _create_user_with_permissions("soundboard-play-user", "pass-1234", {"soundboard.play"})
    play_headers = login_and_get_headers(client, "soundboard-play-user", "pass-1234")
    admin_headers = login_and_get_headers(client, "admin", "changeme")

    audio_file = tmp_path / "beep.mp3"
    audio_file.write_bytes(b"fake-audio")
    create_resp = client.post(
        "/api/soundboard/",
        json={"station_id": 1, "name": "Beep", "file_path": str(audio_file)},
        headers=admin_headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    item_id = int(create_resp.json()["id"])

    played = []

    class DummyPlayer:
        def play(self, item):
            played.append(item["id"])

    async def _noop_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime_api.runtime_registry, "get_sound_effect_player", lambda _station_id: DummyPlayer())
    monkeypatch.setattr(broadcaster_instance, "broadcast_soundboard_event", _noop_broadcast)

    response = client.post(
        "/api/soundboard/play",
        headers=play_headers,
        json={"station_id": 1, "item_id": item_id},
    )

    assert response.status_code == 200
    assert response.json()["playing"] is True
    assert played == [item_id]
