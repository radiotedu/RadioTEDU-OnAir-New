def test_viewer_can_list_studios(client, viewer_token_headers):
    response = client.get("/api/studios?station_id=1", headers=viewer_token_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["station_id"] == 1
    assert len(data["studios"]) >= 1


def test_dj_can_join_and_leave_studio(client, dj_token_headers):
    join_response = client.post("/api/studios/1/join", headers=dj_token_headers)

    assert join_response.status_code == 200
    joined_snapshot = join_response.json()
    assert joined_snapshot["studios"][0]["joined"] is True

    leave_response = client.post("/api/studios/1/leave", headers=dj_token_headers)

    assert leave_response.status_code == 200
    left_snapshot = leave_response.json()
    assert left_snapshot["studios"][0]["joined"] is False


def test_dj_cannot_create_studio(client, dj_token_headers):
    response = client.post(
        "/api/studios",
        headers=dj_token_headers,
        json={"station_id": 1, "name": "Studio B"},
    )

    assert response.status_code == 403


def test_viewer_cannot_post_chat(client, viewer_token_headers):
    response = client.post(
        "/api/studios/1/chat",
        headers=viewer_token_headers,
        json={"message": "hello"},
    )

    assert response.status_code == 403


def test_handoff_returns_conflict_when_target_has_no_active_dj(client, admin_token_headers):
    create_response = client.post(
        "/api/studios",
        headers=admin_token_headers,
        json={"station_id": 1, "name": "Studio B"},
    )
    assert create_response.status_code == 200
    target_studio_id = int(create_response.json()["studio"]["id"])

    handoff_response = client.post(
        "/api/studios/handoff",
        headers=admin_token_headers,
        json={
            "station_id": 1,
            "source_studio_id": 1,
            "target_studio_id": target_studio_id,
        },
    )

    assert handoff_response.status_code == 409


def test_admin_can_update_studio_metadata(client, admin_token_headers):
    create_response = client.post(
        "/api/studios",
        headers=admin_token_headers,
        json={"station_id": 1, "name": "Studio C"},
    )
    assert create_response.status_code == 200
    studio_id = int(create_response.json()["studio"]["id"])

    update_response = client.put(
        f"/api/studios/{studio_id}",
        headers=admin_token_headers,
        json={"name": "Studio C Prime", "description": "Updated"},
    )

    assert update_response.status_code == 200
    payload = update_response.json()
    assert payload["studio"]["name"] == "Studio C Prime"
    assert payload["studio"]["description"] == "Updated"


def test_joined_dj_can_post_and_read_chat_history(client, dj_token_headers):
    join_response = client.post("/api/studios/1/join", headers=dj_token_headers)
    assert join_response.status_code == 200

    post_response = client.post(
        "/api/studios/1/chat",
        headers=dj_token_headers,
        json={"message": "hello studio"},
    )

    assert post_response.status_code == 200
    assert post_response.json()["message"] == "hello studio"

    get_response = client.get("/api/studios/1/chat?limit=10", headers=dj_token_headers)

    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["studio_id"] == 1
    assert payload["messages"][0]["message"] == "hello studio"
