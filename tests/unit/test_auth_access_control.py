def test_protected_api_rejects_missing_token(client):
    response = client.post(
        "/api/queue/push",
        headers={"X-Test-No-Auto-Auth": "1"},
        json={"station_id": 1, "track_id": 77},
    )
    assert response.status_code == 401


def test_viewer_can_read_queue_but_cannot_start_runtime(client, viewer_token_headers):
    queue_res = client.get(
        "/api/queue/items",
        headers=viewer_token_headers,
        params={"station_id": 1},
    )
    runtime_res = client.post(
        "/api/runtime/1/start",
        headers=viewer_token_headers,
        json={"input_uri": "C:/music/a.mp3"},
    )

    assert queue_res.status_code == 200
    assert runtime_res.status_code == 403


def test_producer_can_read_queue_but_not_start_runtime(
    client, producer_token_headers
):
    queue_res = client.get(
        "/api/queue/items",
        headers=producer_token_headers,
        params={"station_id": 1},
    )
    runtime_res = client.post(
        "/api/runtime/1/start",
        headers=producer_token_headers,
        json={"input_uri": "C:/music/a.mp3"},
    )

    assert queue_res.status_code == 200
    assert runtime_res.status_code == 403
