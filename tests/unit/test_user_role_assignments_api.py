from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.user_repo import UserRepository


def _role_template_ids_by_name() -> dict[str, int]:
    init_db()
    conn = get_connection()
    try:
        rows = RbacRepository(conn).list_role_templates(include_inactive=True)
        return {row["name"]: int(row["id"]) for row in rows}
    finally:
        conn.close()


def _create_user(client, payload: dict) -> dict:
    response = client.post("/api/users", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_user_includes_role_template_assignments_and_effective_permissions(
    client, admin_token_headers
):
    role_ids = _role_template_ids_by_name()
    response = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "multi-role-user",
            "display_name": "Multi Role User",
            "password": "pass-1234",
            "role": "viewer",
            "role_template_ids": [
                role_ids["Legacy DJ"],
                role_ids["Legacy Producer"],
            ],
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["legacy_role"] == "viewer"
    assert data["role_template_ids"] == [
        role_ids["Legacy DJ"],
        role_ids["Legacy Producer"],
    ]
    assert "queue.view" in data["effective_permissions"]
    assert "library.edit" in data["effective_permissions"]


def test_update_user_replaces_role_template_assignments(
    client, admin_token_headers
):
    role_ids = _role_template_ids_by_name()
    created = _create_user(
        client,
        {
            "username": "role-update-user",
            "display_name": "Role Update User",
            "password": "pass-1234",
            "role": "viewer",
            "role_template_ids": [role_ids["Legacy Viewer"]],
        },
    )

    response = client.put(
        f"/api/users/{created['id']}",
        headers=admin_token_headers,
        json={
            "role_template_ids": [
                role_ids["Legacy DJ"],
                role_ids["Legacy Producer"],
            ]
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == created["id"]
    assert data["legacy_role"] == "viewer"
    assert data["role_template_ids"] == [
        role_ids["Legacy DJ"],
        role_ids["Legacy Producer"],
    ]
    assert "queue.view" in data["effective_permissions"]
    assert "library.edit" in data["effective_permissions"]


def test_create_user_rejects_unknown_role_template_ids(client, admin_token_headers):
    response = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "bad-role-user",
            "display_name": "Bad Role User",
            "password": "pass-1234",
            "role": "viewer",
            "role_template_ids": [999999],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown role template id: 999999"


def test_create_user_honors_explicit_empty_role_template_ids(
    client, admin_token_headers
):
    response = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "empty-role-user",
            "display_name": "Empty Role User",
            "password": "pass-1234",
            "role": "viewer",
            "role_template_ids": [],
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["role_template_ids"] == []
    assert data["effective_permissions"] == []


def test_create_user_duplicate_username_returns_conflict(
    client, admin_token_headers, monkeypatch
):
    first = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "duplicate-user",
            "display_name": "Duplicate User",
            "password": "pass-1234",
            "role": "viewer",
        },
    )
    assert first.status_code == 201

    monkeypatch.setattr(
        UserRepository,
        "get_user_by_username",
        lambda self, username: None,
    )

    duplicate = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "duplicate-user",
            "display_name": "Duplicate User 2",
            "password": "pass-1234",
            "role": "viewer",
        },
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Username already exists"
