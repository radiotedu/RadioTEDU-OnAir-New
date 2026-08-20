import struct

from app.audio.live_mic_registry import LiveMicRegistry
from app.audio.rtc_mic_session import RtcMicSession


def test_start_rtc_session_stores_session():
    registry = LiveMicRegistry()
    rtc = RtcMicSession(station_id=1)
    rtc._running = True
    user = {"id": 1, "username": "dj-a", "role": "dj"}
    registry.start_rtc_session(1, rtc, user)
    snap = registry.snapshot(1)
    assert snap["transport"] == "webrtc"
    assert snap["transmitting"] is True
    registry.reset()


def test_rtc_session_read_pcm_preferred_over_ws():
    registry = LiveMicRegistry()
    rtc = RtcMicSession(station_id=1)
    rtc._running = True
    pcm = struct.pack("<2h", 500, -500)
    rtc._append_pcm(pcm)
    user = {"id": 1, "username": "dj-a", "role": "dj"}
    registry.start_rtc_session(1, rtc, user)
    result = registry.read_pcm(1, 4)
    assert result == pcm
    registry.reset()


def test_stop_rtc_session_clears_state():
    registry = LiveMicRegistry()
    rtc = RtcMicSession(station_id=1)
    rtc._running = True
    user = {"id": 1, "username": "dj-a", "role": "dj"}
    registry.start_rtc_session(1, rtc, user)
    registry.stop_rtc_session(1)
    snap = registry.snapshot(1)
    assert snap["transport"] == "websocket"
    assert snap["transmitting"] is False
    registry.reset()


def test_push_chunk_noop_when_rtc_active():
    registry = LiveMicRegistry()
    rtc = RtcMicSession(station_id=1)
    rtc._running = True
    user = {"id": 1, "username": "dj-a", "role": "dj"}
    registry.start_rtc_session(1, rtc, user)
    result = registry.push_chunk(1, b"\x00\x01")
    assert result["transport"] == "webrtc"
    # Verify no WS session was spawned
    assert registry._stations[1].get("session") is None
    registry.reset()


def test_snapshot_transport_field_defaults_to_websocket():
    registry = LiveMicRegistry()
    snap = registry.snapshot(1)
    assert snap["transport"] == "websocket"
    registry.reset()


def test_get_rtc_session_returns_session():
    registry = LiveMicRegistry()
    rtc = RtcMicSession(station_id=1)
    rtc._running = True
    user = {"id": 1, "username": "dj-a", "role": "dj"}
    registry.start_rtc_session(1, rtc, user)
    assert registry.get_rtc_session(1) is rtc
    assert registry.get_rtc_session(99) is None
    registry.reset()
