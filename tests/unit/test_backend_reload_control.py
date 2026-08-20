from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import maintenance
from app.api import runtime as runtime_api
from app.auth.jwt_handler import create_access_token
from app.db import get_connection, init_db
from app.main import app
from app.repositories.user_repo import UserRepository
from app.services import backend_reload_control as control


def test_supervisor_capability_and_reload_request_roundtrip(tmp_path: Path):
    token = control.new_supervisor_token()
    control.publish_supervisor_capability(
        token,
        supervisor_pid=42,
        root=tmp_path,
        now=100.0,
    )

    capability = control.read_fresh_supervisor_capability(
        root=tmp_path,
        now=110.0,
    )
    assert capability is not None
    assert capability["supervisor_token"] == token
    assert capability["supervisor_pid"] == 42

    control.write_reload_request(
        token,
        request_id="reload-1",
        backend_instance_id="old-instance",
        root=tmp_path,
        now=110.0,
        not_before_seconds=3.0,
    )
    assert (
        control.consume_due_reload_request(token, root=tmp_path, now=112.9)
        is None
    )
    consumed = control.consume_due_reload_request(
        token,
        root=tmp_path,
        now=113.0,
    )
    assert consumed is not None
    assert consumed["request_id"] == "reload-1"
    assert not control.request_path(tmp_path).exists()


def test_stale_capability_and_wrong_token_are_rejected(tmp_path: Path):
    token = control.new_supervisor_token()
    control.publish_supervisor_capability(
        token,
        supervisor_pid=42,
        root=tmp_path,
        now=10.0,
    )
    assert (
        control.read_fresh_supervisor_capability(root=tmp_path, now=31.0)
        is None
    )

    control.write_reload_request(
        token,
        request_id="reload-2",
        backend_instance_id="old-instance",
        root=tmp_path,
        now=40.0,
        not_before_seconds=0.0,
    )
    assert (
        control.consume_due_reload_request(
            "different-token",
            root=tmp_path,
            now=40.0,
        )
        is None
    )
    assert not control.request_path(tmp_path).exists()


def test_maintenance_reload_requires_loopback_and_fresh_supervisor(monkeypatch):
    remote = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"))
    with pytest.raises(HTTPException) as exc_info:
        maintenance.request_supervised_backend_reload(remote, _user={"role": "admin"})
    assert exc_info.value.status_code == 403

    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr(maintenance, "read_fresh_supervisor_capability", lambda: None)
    with pytest.raises(HTTPException) as exc_info:
        maintenance.request_supervised_backend_reload(local, _user={"role": "admin"})
    assert exc_info.value.status_code == 409


def test_maintenance_reload_preserves_before_writing_request(monkeypatch):
    order = []
    token = control.new_supervisor_token()
    local = SimpleNamespace(client=SimpleNamespace(host="::1"))
    monkeypatch.setattr(
        maintenance,
        "read_fresh_supervisor_capability",
        lambda: {"supervisor_token": token},
    )
    monkeypatch.setattr(
        maintenance,
        "_prepare_for_supervised_reload",
        lambda: order.append("preserve") or {"queue": "preserved"},
    )

    def _write(supervisor_token, **kwargs):
        order.append("request")
        assert supervisor_token == token
        assert kwargs["backend_instance_id"]
        return {}

    monkeypatch.setattr(maintenance, "write_reload_request", _write)
    result = maintenance.request_supervised_backend_reload(
        local,
        _user={"role": "admin"},
    )

    assert order == ["preserve", "request"]
    assert result["accepted"] is True
    assert result["playlist_preserved"] is True


def test_maintenance_reload_route_is_registered_as_post_only():
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/maintenance/backend/reload"
    ]
    assert len(matching) == 1
    assert matching[0].methods == {"POST"}


def test_reload_preparation_stops_audio_before_reconciling(monkeypatch):
    order = []

    class _Connection:
        def close(self):
            order.append("close")

    monkeypatch.setattr(
        runtime_api.worker_loop_manager,
        "stop_all",
        lambda: order.append("loops") or {"stopped": 1},
    )
    monkeypatch.setattr(
        runtime_api.runtime_registry,
        "stop_all",
        lambda: order.append("runtimes") or {"stopped": 1},
    )
    monkeypatch.setattr(
        maintenance,
        "get_connection",
        lambda: order.append("connect") or _Connection(),
    )
    monkeypatch.setattr(
        maintenance,
        "reconcile_all_startup",
        lambda _conn: order.append("reconcile") or {"queue_items": 1},
    )

    result = maintenance._prepare_for_supervised_reload()

    assert order == ["loops", "runtimes", "connect", "reconcile", "close"]
    assert result["reconciliation"] == {"queue_items": 1}


def test_reload_preparation_failure_never_writes_request(monkeypatch):
    token = control.new_supervisor_token()
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    wrote = []
    monkeypatch.setattr(
        maintenance,
        "read_fresh_supervisor_capability",
        lambda: {"supervisor_token": token},
    )
    monkeypatch.setattr(
        maintenance,
        "_prepare_for_supervised_reload",
        lambda: (_ for _ in ()).throw(RuntimeError("stop failed")),
    )
    monkeypatch.setattr(
        maintenance,
        "write_reload_request",
        lambda *args, **kwargs: wrote.append((args, kwargs)),
    )

    with pytest.raises(HTTPException) as exc_info:
        maintenance.request_supervised_backend_reload(
            local,
            _user={"role": "admin"},
        )

    assert exc_info.value.status_code == 503
    assert wrote == []


def test_registered_reload_route_accepts_authenticated_loopback_admin(monkeypatch):
    init_db()
    conn = get_connection()
    try:
        admin = UserRepository(conn).get_user_by_username("admin")
    finally:
        conn.close()
    assert admin is not None
    token = create_access_token(int(admin["id"]), "admin")
    monkeypatch.setattr(
        maintenance,
        "read_fresh_supervisor_capability",
        lambda: {"supervisor_token": control.new_supervisor_token()},
    )
    monkeypatch.setattr(
        maintenance,
        "_prepare_for_supervised_reload",
        lambda: {"queue": "preserved"},
    )
    monkeypatch.setattr(maintenance, "write_reload_request", lambda *a, **k: {})

    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post(
        "/api/maintenance/backend/reload",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json()["playlist_preserved"] is True
