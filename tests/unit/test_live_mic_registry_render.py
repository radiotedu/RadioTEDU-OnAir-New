from app.audio.live_mic_registry import LiveMicRegistry


class _FakeRenderSession:
    def __init__(self, station_id: int, **kwargs):
        self.station_id = int(station_id)
        self.kwargs = dict(kwargs)
        self.running = False
        self.received: list[bytes] = []
        self.stopped = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.stopped = True

    def push_chunk(self, chunk: bytes) -> None:
        self.received.append(bytes(chunk or b""))

    def read_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        payload = b"\x10\x00" * (requested // 2)
        return payload[:requested]

    def snapshot(self) -> dict:
        return {
            "station_id": self.station_id,
            "running": self.running,
            "receiving": bool(self.received),
            "buffer_bytes": sum(len(chunk) for chunk in self.received),
            "level_db": -9.0,
            "peak_db": -3.0,
            "last_error": "",
        }


def test_start_render_transmission_uses_render_transport_and_source_name():
    created = []
    registry = LiveMicRegistry(
        render_session_factory=lambda station_id, **kwargs: created.append(
            _FakeRenderSession(station_id, **kwargs)
        ) or created[-1]
    )

    snap = registry.start_render_transmission(
        1,
        {"id": 7, "username": "admin", "role": "admin"},
        source_name="OmniVoice",
        input_format="s16le",
        sample_rate=24000,
        channels=1,
    )

    assert snap["transport"] == "render"
    assert snap["source_name"] == "OmniVoice"
    assert snap["transmitting"] is True
    assert created[0].kwargs["input_format"] == "s16le"
    assert created[0].kwargs["input_sample_rate"] == 24000
    assert created[0].kwargs["input_channels"] == 1
    registry.reset()


def test_push_render_chunk_feeds_active_render_session():
    created = []
    registry = LiveMicRegistry(
        render_session_factory=lambda station_id, **kwargs: created.append(
            _FakeRenderSession(station_id, **kwargs)
        ) or created[-1]
    )
    registry.start_render_transmission(
        1,
        {"id": 7, "username": "admin", "role": "admin"},
    )

    snap = registry.push_render_chunk(1, b"\x01\x02\x03\x04")

    assert created[0].received == [b"\x01\x02\x03\x04"]
    assert snap["receiving"] is True
    registry.reset()


def test_read_pcm_prefers_render_session_over_other_sources():
    created = []
    registry = LiveMicRegistry(
        render_session_factory=lambda station_id, **kwargs: created.append(
            _FakeRenderSession(station_id, **kwargs)
        ) or created[-1]
    )
    registry.start_render_transmission(
        1,
        {"id": 7, "username": "admin", "role": "admin"},
    )

    result = registry.read_pcm(1, 6)

    assert result == b"\x10\x00\x10\x00\x10\x00"
    registry.reset()


def test_stop_live_input_clears_render_session():
    created = []
    registry = LiveMicRegistry(
        render_session_factory=lambda station_id, **kwargs: created.append(
            _FakeRenderSession(station_id, **kwargs)
        ) or created[-1]
    )
    registry.start_render_transmission(
        1,
        {"id": 7, "username": "admin", "role": "admin"},
    )

    snap = registry.stop_live_input(1, user_id=7)

    assert snap["transport"] == "websocket"
    assert snap["transmitting"] is False
    assert created[0].stopped is True
