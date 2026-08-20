import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth.dependencies import build_auth_user_payload
from app.auth.dependencies import require_any_permission, require_permission, require_show_permission
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.station_repo import StationRepository
from app.repositories.user_repo import UserRepository


def _request() -> Request:
    scope = {"type": "http", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _run(coro):
    return asyncio.run(coro)


def _seed_user_with_real_show_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        stations = StationRepository(conn)
        station_id = stations.create("Station One")
        show_id = ShowRepository(conn).create(station_id, "Morning Show")

        users = UserRepository(conn)
        rbac = RbacRepository(conn)
        user_id = users.create_user(
            username="show-user",
            display_name="Show User",
            password_hash="hash",
            role="producer",
        )
        role_id = rbac.create_role_template("Show Ops", "", False)
        rbac.replace_role_permissions(role_id, {"logs.view"})
        rbac.replace_user_roles(user_id, {role_id})
        ShowRepository(conn).assign(
            show_id,
            user_id,
            role="producer",
            permission_keys={"show.broadcast"},
        )
        rbac.replace_show_permissions(show_id, user_id, {"show.broadcast"})

        user = users.get_user_by_id(user_id)
        payload = build_auth_user_payload(user, conn=conn)
        return payload, show_id
    finally:
        conn.close()


def test_require_permission_allows_user_with_all_requested_permissions(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 1, "effective_permissions": {"logs.view", "queue.view"}}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_permission("logs.view", "queue.view")

    user = _run(dependency(_request()))

    assert user["id"] == 1


def test_require_permission_rejects_when_any_requested_permission_is_missing(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 1, "effective_permissions": {"logs.view"}}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_permission("logs.view", "queue.view")

    with pytest.raises(HTTPException) as exc_info:
        _run(dependency(_request()))

    assert exc_info.value.status_code == 403


def test_require_permission_rejects_empty_or_blank_permission_keys():
    with pytest.raises(ValueError):
        require_permission()

    with pytest.raises(ValueError):
        require_permission(" ")


def test_require_any_permission_allows_user_with_one_matching_permission(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 4, "effective_permissions": {"users.reset_password"}}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_any_permission("users.manage", "users.reset_password")

    user = _run(dependency(_request()))

    assert user["id"] == 4


def test_require_any_permission_rejects_when_no_requested_permission_is_present(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 5, "effective_permissions": {"queue.view"}}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_any_permission("users.manage", "users.reset_password")

    with pytest.raises(HTTPException) as exc_info:
        _run(dependency(_request()))

    assert exc_info.value.status_code == 403


def test_require_show_permission_allows_user_with_show_permission(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 2, "role": "producer", "show_permissions": {7: {"show.broadcast"}}}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_show_permission("show.broadcast")

    user = _run(dependency(_request(), show_id=7))

    assert user["id"] == 2


def test_require_show_permission_allows_admin_without_show_permission(monkeypatch):
    async def _fake_get_current_user(_request):
        return {"id": 3, "role": "admin"}

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_show_permission("show.end")

    user = _run(dependency(_request(), show_id=99))

    assert user["role"] == "admin"


def test_build_auth_user_payload_includes_show_permissions_by_show_id(
    tmp_path, monkeypatch
):
    payload, show_id = _seed_user_with_real_show_permissions(tmp_path, monkeypatch)

    assert payload["legacy_role"] == "producer"
    assert payload["effective_permissions"] == {"logs.view"}
    assert payload["show_permissions"][show_id] == {"show.broadcast"}


def test_require_show_permission_uses_real_auth_payload(tmp_path, monkeypatch):
    payload, show_id = _seed_user_with_real_show_permissions(tmp_path, monkeypatch)

    async def _fake_get_current_user(_request):
        return payload

    monkeypatch.setattr("app.auth.dependencies.get_current_user", _fake_get_current_user)

    dependency = require_show_permission("show.broadcast")

    user = _run(dependency(_request(), show_id=show_id))

    assert user["id"] == payload["id"]
