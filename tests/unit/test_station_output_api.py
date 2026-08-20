from fastapi.testclient import TestClient

from app.main import app


def test_station_output_update_rejects_fifth_local_route():
    client = TestClient(app)
    payload = {"station_id": 5, "local_output_enabled": True, "output_device_id": "dev5"}
    res = client.post("/api/stations/output", json=payload)
    assert res.status_code in (200, 409)


def test_station_output_update_rejects_when_all_outputs_disabled():
    client = TestClient(app)
    payload = {
        "station_id": 11,
        "local_output_enabled": False,
        "icecast_enabled": False,
    }
    res = client.post("/api/stations/output", json=payload)
    assert res.status_code == 400


def test_station_output_can_be_read_back():
    client = TestClient(app)
    payload = {
        "station_id": 12,
        "local_output_enabled": True,
        "output_device_id": "dev12",
        "icecast_enabled": True,
        "icecast_host": "127.0.0.1",
        "icecast_port": 8000,
        "icecast_mount": "/station12",
        "icecast_user": "source",
        "icecast_password": "hackme",
        "icecast_tls_enabled": True,
        "output_gain_db": -2.5,
        "stream_codec_profile": "mp3_128",
        "stream_bitrate_kbps": 128,
    }
    write_res = client.post("/api/stations/output", json=payload)
    assert write_res.status_code == 200

    read_res = client.get("/api/stations/output", params={"station_id": 12})
    assert read_res.status_code == 200
    data = read_res.json()
    assert data["station_id"] == 12
    assert data["local_output_enabled"] is True
    assert data["output_device_id"] == "dev12"
    assert data["icecast_enabled"] is True
    assert data["icecast_mount"] == "/station12"
    assert data["stream_codec_profile"] == "mp3_128"
    assert data["stream_bitrate_kbps"] == 128
    assert data["icecast_tls_enabled"] is True
    assert data["source_protocol"] == "icecast"


def test_station_output_read_defaults_to_speaker_mode_when_missing():
    client = TestClient(app)
    res = client.get("/api/stations/output", params={"station_id": 15})
    assert res.status_code == 200
    data = res.json()
    assert data["station_id"] == 15
    assert data["local_output_enabled"] is True
    assert data["icecast_enabled"] is False
    assert data["output_device_id"] == ""
    assert data["stream_codec_profile"] == "opus_192"
    assert data["stream_bitrate_kbps"] == 192


def test_station_output_allows_windows_default_device_when_local_output_is_enabled():
    client = TestClient(app)
    payload = {
        "station_id": 13,
        "local_output_enabled": True,
        "output_device_id": "",
        "icecast_enabled": False,
    }
    res = client.post("/api/stations/output", json=payload)
    assert res.status_code == 200
    assert res.json()["output"]["output_device_id"] == ""


def test_station_output_requires_mount_when_icecast_is_enabled():
    client = TestClient(app)
    payload = {
        "station_id": 14,
        "local_output_enabled": False,
        "icecast_enabled": True,
        "icecast_mount": "",
    }
    res = client.post("/api/stations/output", json=payload)
    assert res.status_code == 400


def test_station_output_persists_shoutcast_legacy_source_profile():
    client = TestClient(app)
    payload = {
        "station_id": 16,
        "local_output_enabled": False,
        "icecast_enabled": True,
        "icecast_host": "127.0.0.1",
        "icecast_port": 8001,
        "icecast_mount": "",
        "icecast_user": "source",
        "icecast_password": "test-password",
        "source_protocol": "shoutcast",
        "stream_codec_profile": "aac_192",
        "stream_bitrate_kbps": 192,
    }

    write_res = client.post("/api/stations/output", json=payload)
    assert write_res.status_code == 200
    data = client.get(
        "/api/stations/output", params={"station_id": 16}
    ).json()
    assert data["source_protocol"] == "shoutcast"
    assert data["icecast_port"] == 8001
    assert data["stream_codec_profile"] == "aac_192"
    assert data["stream_bitrate_kbps"] == 192


def test_station_output_rejects_opus_for_shoutcast_legacy_source():
    client = TestClient(app)
    response = client.post(
        "/api/stations/output",
        json={
            "station_id": 17,
            "local_output_enabled": False,
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8001,
            "icecast_mount": "",
            "icecast_user": "source",
            "icecast_password": "test-password",
            "source_protocol": "shoutcast",
            "stream_codec_profile": "opus_196",
            "stream_bitrate_kbps": 196,
        },
    )

    assert response.status_code == 400
