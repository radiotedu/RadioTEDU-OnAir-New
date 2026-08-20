def test_admin_can_create_list_and_deactivate_user(client, admin_token_headers):
    created = client.post(
        "/api/users",
        headers=admin_token_headers,
        json={
            "username": "producer-a",
            "display_name": "Producer A",
            "password": "pass-1234",
            "role": "producer",
        },
    )
    assert created.status_code == 201
    user_id = int(created.json()["id"])

    listed = client.get("/api/users", headers=admin_token_headers)
    assert listed.status_code == 200
    assert any(row["username"] == "producer-a" for row in listed.json()["items"])

    deleted = client.delete(f"/api/users/{user_id}", headers=admin_token_headers)
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True


def test_non_admin_cannot_create_user(client, dj_token_headers):
    response = client.post(
        "/api/users",
        headers=dj_token_headers,
        json={
            "username": "viewer-a",
            "display_name": "Viewer A",
            "password": "pass-1234",
            "role": "viewer",
        },
    )
    assert response.status_code == 403
