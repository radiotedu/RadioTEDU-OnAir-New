from app.auth.password import hash_password
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.user_repo import UserRepository


def _login_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_user_with_role_manage_permission(username: str, password: str) -> None:
    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        rbac = RbacRepository(conn)
        user_id = users.create_user(
            username=username,
            display_name="Role Manager",
            password_hash=hash_password(password),
            role="viewer",
        )
        role_id = rbac.create_role_template("Role Managers", "", False)
        rbac.replace_role_permissions(role_id, {"roles.manage"})
        rbac.replace_user_roles(user_id, {role_id})
    finally:
        conn.close()


def _create_user_with_users_manage_permission(username: str, password: str) -> None:
    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        rbac = RbacRepository(conn)
        user_id = users.create_user(
            username=username,
            display_name="Users Manager",
            password_hash=hash_password(password),
            role="viewer",
        )
        role_id = rbac.create_role_template("Users Managers", "", False)
        rbac.replace_role_permissions(role_id, {"users.manage"})
        rbac.replace_user_roles(user_id, {role_id})
    finally:
        conn.close()


def _create_viewer_user(username: str, password: str) -> None:
    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        users.create_user(
            username=username,
            display_name="Viewer",
            password_hash=hash_password(password),
            role="viewer",
        )
    finally:
        conn.close()


def test_user_with_roles_manage_permission_can_crud_role_templates(client):
    _create_user_with_role_manage_permission("role-manager", "pass-1234")
    headers = _login_headers(client, "role-manager", "pass-1234")

    listed = client.get("/api/roles", headers=headers)
    assert listed.status_code == 200
    permission_groups = listed.json()["permission_groups"]
    assert permission_groups["roles"] == ["roles.manage"]
    assert permission_groups["program"] == ["program.panel.open"]
    legacy_admin = next(
        item for item in listed.json()["items"] if item["name"] == "Legacy Admin"
    )
    assert isinstance(legacy_admin["permission_keys"], list)
    assert "roles.manage" in legacy_admin["permission_keys"]

    created = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Music Team",
            "description": "Library and playlist operators",
            "permission_keys": ["library.view", "library.edit", "playlists.view"],
        },
    )
    assert created.status_code == 201
    created_data = created.json()
    assert created_data["name"] == "Music Team"
    assert created_data["description"] == "Library and playlist operators"
    assert created_data["is_system"] is False
    assert created_data["is_active"] is True
    assert created_data["permission_keys"] == [
        "library.edit",
        "library.view",
        "playlists.view",
    ]

    role_id = int(created_data["id"])
    updated = client.put(
        f"/api/roles/{role_id}",
        headers=headers,
        json={
            "description": "Updated operators",
            "permission_keys": ["library.view", "queue.view"],
        },
    )
    assert updated.status_code == 200
    updated_data = updated.json()
    assert updated_data["description"] == "Updated operators"
    assert updated_data["permission_keys"] == ["library.view", "queue.view"]

    deleted = client.delete(f"/api/roles/{role_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}

    relisted = client.get("/api/roles", headers=headers)
    assert relisted.status_code == 200
    created_template = next(
        item for item in relisted.json()["items"] if int(item["id"]) == role_id
    )
    assert created_template["is_active"] is False


def test_permission_only_update_replaces_role_permissions(client):
    _create_user_with_role_manage_permission("role-manager-perms", "pass-1234")
    headers = _login_headers(client, "role-manager-perms", "pass-1234")

    created = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Queue Team",
            "description": "Queue operators",
            "permission_keys": ["queue.view"],
        },
    )
    assert created.status_code == 201
    role_id = int(created.json()["id"])

    updated = client.put(
        f"/api/roles/{role_id}",
        headers=headers,
        json={"permission_keys": ["queue.edit", "logs.view"]},
    )
    assert updated.status_code == 200
    assert updated.json()["permission_keys"] == ["logs.view", "queue.edit"]


def test_duplicate_role_name_create_returns_conflict(client):
    _create_user_with_role_manage_permission("role-manager-create-conflict", "pass-1234")
    headers = _login_headers(client, "role-manager-create-conflict", "pass-1234")

    first = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Duplicate Team",
            "description": "First template",
            "permission_keys": ["queue.view"],
        },
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Duplicate Team",
            "description": "Second template",
            "permission_keys": ["logs.view"],
        },
    )
    assert duplicate.status_code == 409


def test_duplicate_role_name_rename_returns_conflict(client):
    _create_user_with_role_manage_permission("role-manager-rename-conflict", "pass-1234")
    headers = _login_headers(client, "role-manager-rename-conflict", "pass-1234")

    first = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Alpha Team",
            "description": "First template",
            "permission_keys": ["queue.view"],
        },
    )
    second = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Beta Team",
            "description": "Second template",
            "permission_keys": ["logs.view"],
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201

    renamed = client.put(
        f"/api/roles/{second.json()['id']}",
        headers=headers,
        json={"name": "Alpha Team"},
    )
    assert renamed.status_code == 409


def test_system_role_template_update_is_rejected(client):
    _create_user_with_role_manage_permission("role-manager-system-update", "pass-1234")
    headers = _login_headers(client, "role-manager-system-update", "pass-1234")

    listed = client.get("/api/roles", headers=headers)
    legacy_admin = next(
        item for item in listed.json()["items"] if item["name"] == "Legacy Admin"
    )

    response = client.put(
        f"/api/roles/{legacy_admin['id']}",
        headers=headers,
        json={"description": "Modified admin template"},
    )
    assert response.status_code == 400


def test_roles_api_requires_roles_manage_permission(client):
    _create_viewer_user("plain-viewer", "pass-1234")
    headers = _login_headers(client, "plain-viewer", "pass-1234")

    response = client.get("/api/roles", headers=headers)
    assert response.status_code == 403


def test_roles_api_allows_users_manage_read_only_access(client):
    _create_user_with_users_manage_permission("users-manager", "pass-1234")
    headers = _login_headers(client, "users-manager", "pass-1234")

    listed = client.get("/api/roles", headers=headers)
    assert listed.status_code == 200

    created = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Should Fail",
            "permission_keys": ["queue.view"],
        },
    )
    assert created.status_code == 403


def test_roles_api_rejects_invalid_permission_keys(client):
    _create_user_with_role_manage_permission("role-manager-invalid", "pass-1234")
    headers = _login_headers(client, "role-manager-invalid", "pass-1234")

    response = client.post(
        "/api/roles",
        headers=headers,
        json={
            "name": "Bad Role",
            "permission_keys": ["library.view", "not.a.permission"],
        },
    )
    assert response.status_code == 400


def test_roles_api_does_not_deactivate_system_templates(client):
    _create_user_with_role_manage_permission("role-manager-system", "pass-1234")
    headers = _login_headers(client, "role-manager-system", "pass-1234")

    listed = client.get("/api/roles", headers=headers)
    legacy_admin = next(
        item for item in listed.json()["items"] if item["name"] == "Legacy Admin"
    )

    response = client.delete(f"/api/roles/{legacy_admin['id']}", headers=headers)
    assert response.status_code == 400
