from app.audio.live_mic_registry import live_mic_registry


class _FakeRenderSession:
    def __init__(self, station_id: int, **kwargs):
        self.station_id = int(station_id)
        self.kwargs = dict(kwargs)
        self.running = False
        self.received: list[bytes] = []

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def push_chunk(self, chunk: bytes) -> None:
        self.received.append(bytes(chunk or b""))

    def read_pcm(self, num_bytes: int) -> bytes:
        return b"\x00" * max(0, int(num_bytes))

    def snapshot(self) -> dict:
        return {
            "station_id": self.station_id,
            "running": self.running,
            "receiving": bool(self.received),
            "buffer_bytes": sum(len(chunk) for chunk in self.received),
            "level_db": -12.0,
            "peak_db": -6.0,
            "last_error": "",
        }


def test_live_render_api_start_chunk_stop(client, admin_token_headers):
    created = []
    original_factory = live_mic_registry._render_session_factory
    live_mic_registry.reset()
    live_mic_registry._render_session_factory = (
        lambda station_id, **kwargs: created.append(_FakeRenderSession(station_id, **kwargs)) or created[-1]
    )

    join_response = client.post("/api/studios/1/join", headers=admin_token_headers)
    assert join_response.status_code == 200

    start_response = client.post(
        "/api/audio/live/render/start",
        headers=admin_token_headers,
        json={
            "station_id": 1,
            "source_name": "OmniVoice",
            "input_format": "s16le",
            "sample_rate": 24000,
            "channels": 1,
        },
    )

    assert start_response.status_code == 200
    assert start_response.json()["transport"] == "render"
    assert start_response.json()["source_name"] == "OmniVoice"

    chunk_response = client.post(
        "/api/audio/live/render/chunk?station_id=1",
        headers=admin_token_headers,
        content=b"\x01\x02\x03\x04",
    )

    assert chunk_response.status_code == 200
    assert created[0].received == [b"\x01\x02\x03\x04"]

    stop_response = client.post(
        "/api/audio/live/render/stop",
        headers=admin_token_headers,
        json={"station_id": 1},
    )

    assert stop_response.status_code == 200
    assert stop_response.json()["transmitting"] is False
    live_mic_registry.reset()
    live_mic_registry._render_session_factory = original_factory
