def test_live_audio_status_returns_station_snapshot_for_admin(client, admin_token_headers):
    response = client.get(
        "/api/audio/live/status",
        headers=admin_token_headers,
        params={"station_id": 1},
    )

    assert response.status_code == 200
    assert response.json() == {
        "station_id": 1,
        "live_input_enabled": False,
        "transmitting": False,
        "active_user": None,
        "program_music_mode": "normal",
        "mic_gain": 1.0,
        "music_gain": 1.0,
        "duck_level": 0.15,
    }


def test_live_audio_status_allows_dj_but_forbids_producer_and_viewer(
    client,
    dj_token_headers,
    producer_token_headers,
    viewer_token_headers,
):
    dj_response = client.get(
        "/api/audio/live/status",
        headers=dj_token_headers,
        params={"station_id": 1},
    )
    producer_response = client.get(
        "/api/audio/live/status",
        headers=producer_token_headers,
        params={"station_id": 1},
    )
    viewer_response = client.get(
        "/api/audio/live/status",
        headers=viewer_token_headers,
        params={"station_id": 1},
    )

    assert dj_response.status_code == 200
    assert producer_response.status_code == 403
    assert viewer_response.status_code == 403


def test_live_audio_settings_update_persists_for_admin(client, admin_token_headers):
    payload = {
        "station_id": 1,
        "program_music_mode": "duck",
        "mic_gain": 1.25,
        "music_gain": 0.9,
        "duck_level": 0.2,
    }

    response = client.put(
        "/api/audio/live/settings",
        headers=admin_token_headers,
        json=payload,
    )

    assert response.status_code == 200
    assert response.json() == payload
