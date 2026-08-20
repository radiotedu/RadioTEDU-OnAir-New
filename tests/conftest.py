from fastapi.testclient import TestClient
import pytest
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from app.main import app

_PUBLIC_API_PATHS = {
    "/api/health/live",
    "/api/health/ready",
    "/api/health-wall",
    "/api/monitor/snapshot",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/public/stations",
    "/api/public/campaign",
    "/api/public/campaign/vote",
    "/api/watchdog/status",
    "/api/watchdog/repair",
    "/api/watchdog/report",
}


def _normalize_api_path(path: str) -> str:
    normalized = str(path or "").strip()
    if normalized.endswith("/") and normalized != "/":
        return normalized.rstrip("/")
    return normalized


def _path_from_url(url) -> str:
    text = str(url or "")
    if "://" in text:
        return str(urlsplit(text).path or "")
    return text.split("?", 1)[0]


def _should_auto_auth(path: str) -> bool:
    normalized = _normalize_api_path(path)
    return normalized.startswith("/api/") and normalized not in _PUBLIC_API_PATHS


def login_and_get_headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _ensure_user(username: str, display_name: str, password: str, role: str) -> None:
    from app.auth.password import hash_password
    from app.db import get_connection, init_db
    from app.repositories.user_repo import UserRepository

    init_db()
    conn = get_connection()
    try:
        repo = UserRepository(conn)
        existing = repo.get_user_by_username(username)
        if existing is None:
            repo.create_user(username, display_name, hash_password(password), role)
            return
        repo.update_user(
            int(existing["id"]),
            display_name=display_name,
            password_hash=hash_password(password),
            role=role,
            is_active=1,
        )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _auto_auth_testclient(monkeypatch, tmp_path):
    # Some legacy tests construct TestClient directly instead of using the
    # client fixture below. Give every test its own database before any
    # TestClient can start the application so credentials, queues, and schema
    # state cannot leak between tests or into the developer's runtime data.
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_INITIAL_ADMIN_PASSWORD", "changeme")
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE",
        str(tmp_path / "station-credentials.json"),
    )
    monkeypatch.setenv("CLEANROOM_DISABLE_LIBRARY_WATCHER", "1")
    original_request = TestClient.request

    def patched_request(self, method, url, *args, **kwargs):
        path = _path_from_url(url)
        headers = dict(kwargs.get("headers") or {})
        skip_auto_auth = str(headers.pop("X-Test-No-Auto-Auth", "")).strip() == "1"
        has_auth = any(str(key).lower() == "authorization" for key in headers)

        if not skip_auto_auth and _should_auto_auth(path) and not has_auth:
            token = str(getattr(self, "_cleanroom_test_admin_token", "") or "")
            if not token:
                login_response = original_request(
                    self,
                    "POST",
                    "/api/auth/login",
                    json={"username": "admin", "password": "changeme"},
                )
                assert login_response.status_code == 200, login_response.text
                token = str(login_response.json()["access_token"])
                setattr(self, "_cleanroom_test_admin_token", token)
            headers["Authorization"] = f"Bearer {token}"

        if headers or "headers" in kwargs:
            kwargs["headers"] = headers
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", patched_request)


@pytest.fixture(autouse=True)
def _disable_dependency_bootstrap_for_app_tests(monkeypatch, request):
    if str(request.node.fspath).endswith("test_dependency_bootstrap.py"):
        return
    monkeypatch.setattr("app.main.bootstrap_dependencies", lambda: {})


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    from app.main import login_rate_limiter

    login_rate_limiter.reset()
    yield
    login_rate_limiter.reset()


@pytest.fixture
def tmp_path(request):
    token = re.sub(r"[^A-Za-z0-9]+", "", request.node.name)[:12] or "test"
    path = Path(".tmp") / "pt" / f"{token}-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path.resolve()
    finally:
        shutil.rmtree(path, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    if config.pluginmanager.hasplugin("playwright"):
        return
    skip_playwright = pytest.mark.skip(reason="pytest-playwright plugin is not installed")
    for item in items:
        path = str(getattr(item, "fspath", "") or "").replace("\\", "/")
        if "/tests/playwright/" in path:
            item.add_marker(skip_playwright)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def admin_token_headers(client):
    return login_and_get_headers(client, "admin", "changeme")


@pytest.fixture
def dj_token_headers(client):
    _ensure_user("dj-user", "DJ User", "pass-1234", "dj")
    return login_and_get_headers(client, "dj-user", "pass-1234")


@pytest.fixture
def producer_token_headers(client):
    _ensure_user("producer-user", "Producer User", "pass-1234", "producer")
    return login_and_get_headers(client, "producer-user", "pass-1234")


@pytest.fixture
def viewer_token_headers(client):
    _ensure_user("viewer-user", "Viewer User", "pass-1234", "viewer")
    return login_and_get_headers(client, "viewer-user", "pass-1234")
