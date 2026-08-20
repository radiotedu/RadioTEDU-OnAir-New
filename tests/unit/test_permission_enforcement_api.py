from app.auth.password import hash_password
from app.config import get_db_path
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


def _create_user_with_permissions(
    username: str,
    password: str,
    permission_keys: set[str],
) -> int:
    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        rbac = RbacRepository(conn)
        user_id = users.create_user(
            username=username,
            display_name=username.replace("-", " ").title(),
            password_hash=hash_password(password),
            role="viewer",
        )
        role_id = rbac.create_role_template(f"{username}-role", "", False)
        rbac.replace_role_permissions(role_id, set(permission_keys))
        rbac.replace_user_roles(user_id, {role_id})
        return user_id
    finally:
        conn.close()


def test_user_with_users_manage_permission_can_list_users(client):
    _create_user_with_permissions(
        "users-manager-perm",
        "pass-1234",
        {"users.manage"},
    )
    headers = _login_headers(client, "users-manager-perm", "pass-1234")

    response = client.get("/api/users", headers=headers)

    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_user_with_soundboard_manage_permission_can_create_soundboard_item(client):
    _create_user_with_permissions(
        "soundboard-manager-perm",
        "pass-1234",
        {"soundboard.manage"},
    )
    headers = _login_headers(client, "soundboard-manager-perm", "pass-1234")

    response = client.post(
        "/api/soundboard/",
        headers=headers,
        json={
            "station_id": 1,
            "name": "Airhorn",
            "file_path": "/tmp/airhorn.mp3",
        },
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Airhorn"


def test_user_with_shows_manage_permission_can_create_show(client):
    _create_user_with_permissions(
        "shows-manager-perm",
        "pass-1234",
        {"shows.manage"},
    )
    headers = _login_headers(client, "shows-manager-perm", "pass-1234")

    response = client.post(
        "/api/shows/",
        headers=headers,
        json={"station_id": 1, "name": "Night Shift"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Night Shift"


def test_user_with_stations_create_permission_can_create_station(client):
    _create_user_with_permissions(
        "station-creator-perm",
        "pass-1234",
        {"stations.create"},
    )
    headers = _login_headers(client, "station-creator-perm", "pass-1234")

    response = client.post(
        "/api/stations",
        headers=headers,
        json={"name": "Remote Studio"},
    )

    assert response.status_code == 200
    assert response.json()["id"] > 0


def test_user_with_stations_edit_permission_can_update_station_output(client):
    _create_user_with_permissions(
        "station-editor-perm",
        "pass-1234",
        {"stations.edit"},
    )
    headers = _login_headers(client, "station-editor-perm", "pass-1234")

    response = client.post(
        "/api/stations/output",
        headers=headers,
        json={
            "station_id": 1,
            "local_output_enabled": False,
            "output_device_id": "",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/station1",
            "icecast_user": "source",
            "icecast_password": "secret",
            "output_gain_db": 0.0,
        },
    )

    assert response.status_code == 200
    assert response.json()["output"]["icecast_mount"] == "/station1"


def test_user_with_stations_delete_permission_can_delete_station(client):
    _create_user_with_permissions(
        "station-deleter-perm",
        "pass-1234",
        {"stations.delete"},
    )
    admin_headers = _login_headers(client, "admin", "changeme")
    created = client.post(
        "/api/stations",
        headers=admin_headers,
        json={"name": "Delete Me"},
    )
    assert created.status_code == 200, created.text
    station_id = int(created.json()["id"])
    station_uploads = get_db_path().parent / "uploads" / f"station-{station_id}"
    station_downloads = get_db_path().parent / "downloads" / f"station-{station_id}"
    station_uploads.mkdir(parents=True)
    station_downloads.mkdir(parents=True)
    (station_uploads / "test-jingle.mp3").write_bytes(b"temporary")
    (station_downloads / "test-track.mp3").write_bytes(b"temporary")

    headers = _login_headers(client, "station-deleter-perm", "pass-1234")
    response = client.delete(f"/api/stations/{station_id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["deleted_station_id"] == station_id
    assert response.json()["media_cleanup"] == {
        "ok": True,
        "removed": ["uploads", "downloads"],
        "errors": [],
    }
    assert not station_uploads.exists()
    assert not station_downloads.exists()


def test_stations_create_permission_cannot_delete_station(client):
    _create_user_with_permissions(
        "station-create-only",
        "pass-1234",
        {"stations.create"},
    )
    admin_headers = _login_headers(client, "admin", "changeme")
    created = client.post(
        "/api/stations",
        headers=admin_headers,
        json={"name": "Protected Delete"},
    )
    assert created.status_code == 200, created.text
    station_id = int(created.json()["id"])

    headers = _login_headers(client, "station-create-only", "pass-1234")
    response = client.delete(f"/api/stations/{station_id}", headers=headers)

    assert response.status_code == 403


def test_delete_station_rejects_the_last_station_over_http(client):
    _create_user_with_permissions(
        "station-last-delete",
        "pass-1234",
        {"stations.delete"},
    )
    headers = _login_headers(client, "station-last-delete", "pass-1234")
    stations = client.get("/api/stations", headers=headers)
    assert stations.status_code == 200, stations.text
    station_id = int(stations.json()["stations"][0]["id"])

    response = client.delete(f"/api/stations/{station_id}", headers=headers)

    assert response.status_code == 400
    assert "last station" in response.json()["detail"]


def test_stations_view_permission_redacts_output_password(client):
    _create_user_with_permissions(
        "station-viewer-perm",
        "pass-1234",
        {"stations.view"},
    )
    admin_headers = _login_headers(client, "admin", "changeme")
    update_response = client.post(
        "/api/stations/output",
        headers=admin_headers,
        json={
            "station_id": 1,
            "local_output_enabled": False,
            "output_device_id": "",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/station1",
            "icecast_user": "source",
            "icecast_password": "top-secret",
            "output_gain_db": 0.0,
        },
    )
    assert update_response.status_code == 200, update_response.text

    headers = _login_headers(client, "station-viewer-perm", "pass-1234")
    response = client.get("/api/stations/output?station_id=1", headers=headers)

    assert response.status_code == 200
    assert response.json()["icecast_password"] == ""


def test_user_with_downloads_use_permission_can_read_ytdlp_settings(client):
    _create_user_with_permissions(
        "downloads-operator-perm",
        "pass-1234",
        {"downloads.use"},
    )
    headers = _login_headers(client, "downloads-operator-perm", "pass-1234")

    response = client.get("/api/library/import/ytdlp/settings?station_id=1", headers=headers)

    assert response.status_code == 200
    assert response.json()["station"]["id"] == 1


def test_user_with_logs_view_permission_can_list_logs(client):
    _create_user_with_permissions(
        "logs-reader-perm",
        "pass-1234",
        {"logs.view"},
    )
    headers = _login_headers(client, "logs-reader-perm", "pass-1234")

    response = client.get("/api/logs?station_id=1", headers=headers)

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_logs_view_permission_cannot_create_event(client):
    _create_user_with_permissions(
        "logs-view-only",
        "pass-1234",
        {"logs.view"},
    )
    headers = _login_headers(client, "logs-view-only", "pass-1234")

    response = client.post(
        "/api/events",
        headers=headers,
        json={"station_id": 1, "event_type": "test", "payload": {}},
    )

    assert response.status_code == 403


def test_user_with_reset_password_permission_can_list_users_and_reset_password(client):
    _create_user_with_permissions(
        "reset-password-operator",
        "pass-1234",
        {"users.reset_password"},
    )
    target_user_id = _create_user_with_permissions(
        "password-reset-target",
        "pass-1234",
        set(),
    )
    headers = _login_headers(client, "reset-password-operator", "pass-1234")

    listed = client.get("/api/users", headers=headers)
    assert listed.status_code == 200
    assert any(int(item["id"]) == target_user_id for item in listed.json()["items"])

    response = client.post(
        f"/api/users/{target_user_id}/reset-password",
        headers=headers,
        json={"new_password": "new-pass-1234"},
    )

    assert response.status_code == 200
    assert response.json()["detail"] == "Password reset"


def test_user_with_shows_manage_permission_can_list_station_shows(client):
    _create_user_with_permissions(
        "shows-list-manager-perm",
        "pass-1234",
        {"shows.manage"},
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Alpha Show"},
    )
    client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Beta Show"},
    )

    headers = _login_headers(client, "shows-list-manager-perm", "pass-1234")
    response = client.get("/api/shows/?station_id=1", headers=headers)

    assert response.status_code == 200
    assert [show["name"] for show in response.json()] == ["Alpha Show", "Beta Show"]


def test_user_with_show_assign_manage_permission_can_list_station_shows(client):
    _create_user_with_permissions(
        "show-assign-list-manager-perm",
        "pass-1234",
        {"show.assign.manage"},
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Gamma Show"},
    )
    client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Delta Show"},
    )

    headers = _login_headers(client, "show-assign-list-manager-perm", "pass-1234")
    response = client.get("/api/shows/?station_id=1", headers=headers)

    assert response.status_code == 200
    assert [show["name"] for show in response.json()] == ["Delta Show", "Gamma Show"]


def test_user_with_show_assign_manage_permission_can_assign_show_user(client):
    target_user_id = _create_user_with_permissions(
        "show-assignment-target",
        "pass-1234",
        set(),
    )
    _create_user_with_permissions(
        "show-assign-manager-perm",
        "pass-1234",
        {"show.assign.manage"},
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    create_response = client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Assignment Test"},
    )
    assert create_response.status_code == 200, create_response.text
    show_id = int(create_response.json()["id"])

    headers = _login_headers(client, "show-assign-manager-perm", "pass-1234")
    response = client.post(
        f"/api/shows/{show_id}/assign",
        headers=headers,
        json={
            "user_id": target_user_id,
            "role": "dj",
            "permission_keys": ["show.broadcast"],
        },
    )

    assert response.status_code == 200
    assignments = response.json()["assignments"]
    assert len(assignments) == 1
    assert assignments[0]["user_id"] == target_user_id
    assert assignments[0]["permission_keys"] == ["show.broadcast"]


def test_user_with_show_assign_manage_permission_can_list_show_assignments(client):
    target_user_id = _create_user_with_permissions(
        "show-assignment-list-target",
        "pass-1234",
        set(),
    )
    _create_user_with_permissions(
        "show-assignment-list-manager",
        "pass-1234",
        {"show.assign.manage"},
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    create_response = client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Assignment List Test"},
    )
    assert create_response.status_code == 200, create_response.text
    show_id = int(create_response.json()["id"])

    assign_response = client.post(
        f"/api/shows/{show_id}/assign",
        headers=admin_headers,
        json={
            "user_id": target_user_id,
            "role": "dj",
            "permission_keys": ["show.broadcast"],
        },
    )
    assert assign_response.status_code == 200, assign_response.text

    headers = _login_headers(client, "show-assignment-list-manager", "pass-1234")
    response = client.get(f"/api/shows/{show_id}/assignments", headers=headers)

    assert response.status_code == 200
    assert response.json()[0]["user_id"] == target_user_id


def test_user_with_show_assign_manage_permission_can_list_assignment_candidates(client):
    _create_user_with_permissions(
        "show-assignment-candidate-manager",
        "pass-1234",
        {"show.assign.manage"},
    )

    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        active_user_id = users.create_user(
            username="show-assignment-candidate-active",
            display_name="Assignment Candidate Active",
            password_hash=hash_password("pass-1234"),
            role="producer",
        )
        inactive_user_id = users.create_user(
            username="show-assignment-candidate-inactive",
            display_name="Assignment Candidate Inactive",
            password_hash=hash_password("pass-1234"),
            role="viewer",
        )
        users.deactivate_user(inactive_user_id)
        assert active_user_id > 0
    finally:
        conn.close()

    admin_headers = _login_headers(client, "admin", "changeme")
    create_response = client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Assignment Candidate Test"},
    )
    assert create_response.status_code == 200, create_response.text
    show_id = int(create_response.json()["id"])

    headers = _login_headers(client, "show-assignment-candidate-manager", "pass-1234")
    response = client.get(f"/api/shows/{show_id}/assignment-candidates", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["items"], list)
    assert any(item["username"] == "show-assignment-candidate-active" for item in payload["items"])
    assert not any(item["username"] == "show-assignment-candidate-inactive" for item in payload["items"])
    assert all(set(item.keys()) <= {"id", "username", "display_name", "role", "is_active"} for item in payload["items"])


def test_user_without_show_assign_manage_permission_cannot_list_assignment_candidates(client):
    _create_user_with_permissions(
        "show-assignment-candidate-viewer",
        "pass-1234",
        set(),
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    create_response = client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Assignment Candidate Viewer Test"},
    )
    assert create_response.status_code == 200, create_response.text
    show_id = int(create_response.json()["id"])

    headers = _login_headers(client, "show-assignment-candidate-viewer", "pass-1234")
    response = client.get(f"/api/shows/{show_id}/assignment-candidates", headers=headers)

    assert response.status_code == 403


def test_user_without_show_assign_manage_permission_cannot_list_show_assignments(client):
    target_user_id = _create_user_with_permissions(
        "show-assignment-read-target",
        "pass-1234",
        set(),
    )
    _create_user_with_permissions(
        "show-assignment-read-viewer",
        "pass-1234",
        set(),
    )

    admin_headers = _login_headers(client, "admin", "changeme")
    create_response = client.post(
        "/api/shows/",
        headers=admin_headers,
        json={"station_id": 1, "name": "Assignment Read Test"},
    )
    assert create_response.status_code == 200, create_response.text
    show_id = int(create_response.json()["id"])

    assign_response = client.post(
        f"/api/shows/{show_id}/assign",
        headers=admin_headers,
        json={
            "user_id": target_user_id,
            "role": "dj",
            "permission_keys": ["show.broadcast"],
        },
    )
    assert assign_response.status_code == 200, assign_response.text

    headers = _login_headers(client, "show-assignment-read-viewer", "pass-1234")
    response = client.get(f"/api/shows/{show_id}/assignments", headers=headers)

    assert response.status_code == 403
