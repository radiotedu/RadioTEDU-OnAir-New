from app.db import get_connection


def _stale_legacy_templates() -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE role_templates SET is_active = 0 WHERE name LIKE 'Legacy %'")
        conn.commit()
    finally:
        conn.close()


def _assert_enriched_auth_user(user: dict) -> None:
    assert user["legacy_role"] == "admin"
    assert isinstance(user["effective_permissions"], list)
    assert isinstance(user["role_template_ids"], list)
    assert user["effective_permissions"]
    assert user["role_template_ids"]


def test_login_returns_access_and_refresh_tokens(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["user"]["username"] == "admin"
    assert payload["access_token"]
    assert payload["refresh_token"]


def test_auth_contract_recovers_legacy_permissions_on_existing_v6_db(client):
    _stale_legacy_templates()

    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    assert login.status_code == 200
    login_payload = login.json()
    _assert_enriched_auth_user(login_payload["user"])

    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login_payload['access_token']}"},
    )
    assert me.status_code == 200
    _assert_enriched_auth_user(me.json())

    refreshed = client.post(
        "/api/auth/refresh",
        json={"refresh_token": login_payload["refresh_token"]},
    )
    assert refreshed.status_code == 200
    assert isinstance(refreshed.json()["user"]["effective_permissions"], list)
    assert isinstance(refreshed.json()["user"]["role_template_ids"], list)
    assert refreshed.json()["user"]["effective_permissions"]
    assert refreshed.json()["user"]["role_template_ids"]


def test_login_response_is_marked_no_store(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"


def test_refresh_rotates_refresh_token(client):
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    assert login.status_code == 200

    refreshed = client.post(
        "/api/auth/refresh",
        json={"refresh_token": login.json()["refresh_token"]},
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != login.json()["refresh_token"]


def test_me_returns_authenticated_user(client):
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    token = login.json()["access_token"]

    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert me.status_code == 200
    assert me.json()["username"] == "admin"


def test_password_update_accepts_current_password(client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.auth.get_data_root", lambda: tmp_path / "data")
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    token = login.json()["access_token"]
    initial_password_file = tmp_path / "data" / "initial-admin-password.txt"
    initial_password_file.parent.mkdir(parents=True, exist_ok=True)
    initial_password_file.write_text("obsolete bootstrap credential", encoding="utf-8")

    response = client.put(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "changeme", "new_password": "radio"},
    )

    assert response.status_code == 200
    assert response.json()["detail"] == "Password updated"
    assert not initial_password_file.exists()


def test_password_update_rejects_fewer_than_five_characters(client):
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    token = login.json()["access_token"]

    response = client.put(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "changeme", "new_password": "four"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Password too short"


def test_logout_returns_logged_out(client):
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "changeme"},
    )
    token = login.json()["access_token"]

    response = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["detail"] == "Logged out"


def test_login_rate_limit_returns_429_when_threshold_exceeded(client, monkeypatch):
    from app.main import login_rate_limiter

    login_rate_limiter.reset()
    monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")

    first = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    second = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    third = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["detail"] == "Too many login attempts"
    assert str(third.headers.get("x-request-id", "")).strip() != ""


def test_login_rate_limit_uses_forwarded_client_ip_when_proxy_headers_trusted(
    client, monkeypatch
):
    from app.main import login_rate_limiter

    login_rate_limiter.reset()
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")

    first = client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "198.51.100.10"},
        json={"username": "admin", "password": "changeme"},
    )
    second = client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "198.51.100.11"},
        json={"username": "admin", "password": "changeme"},
    )

    assert first.status_code == 200
    assert second.status_code == 200


def test_login_session_does_not_store_forwarded_client_ip_when_proxy_headers_trusted(
    client, monkeypatch
):
    from app.db import get_connection
    from app.main import login_rate_limiter
    from app.repositories.user_repo import SessionRepository

    login_rate_limiter.reset()
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "1")
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")

    first = client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "198.51.100.10"},
        json={"username": "admin", "password": "changeme"},
    )
    second = client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "198.51.100.11"},
        json={"username": "admin", "password": "changeme"},
    )

    assert first.status_code == 200
    assert second.status_code == 200

    conn = get_connection()
    try:
        session = SessionRepository(conn).get_session_by_token(first.json()["refresh_token"])
        assert session is not None
        assert str(session["ip_address"]) != "198.51.100.10"
        assert str(session["ip_address"]).strip() != ""
    finally:
        conn.close()
