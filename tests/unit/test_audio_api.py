from fastapi.testclient import TestClient

from app.main import app
import app.api.audio as audio_api


def test_audio_devices_endpoint(monkeypatch):
    monkeypatch.setattr(
        audio_api,
        "list_output_devices",
        lambda ffmpeg_bin="ffmpeg": [
            "Speakers (USB)",
            {"id": "hdmi-1", "label": "Monitor (HDMI)"},
        ],
    )
    client = TestClient(app)
    res = client.get("/api/audio/devices")
    assert res.status_code == 200
    devices = res.json()["devices"]
    assert devices[0]["id"] == "Speakers (USB)"
    assert devices[0]["label"] == "Speakers (USB)"
    assert devices[1]["id"] == "hdmi-1"
    assert devices[1]["label"] == "Monitor (HDMI)"
