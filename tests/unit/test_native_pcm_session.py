import struct

import pytest

from app.audio.native_pcm_session import NativePcmSession
from app.audio.live_mic_registry import LiveMicRegistry


def test_native_pcm_is_bit_exact_and_never_starts_decoder(monkeypatch):
    monkeypatch.setattr('app.audio.mic_session.resolve_binary', lambda _: None)
    monkeypatch.setattr('app.audio.mic_session.subprocess.Popen', lambda *a, **k: pytest.fail('Native PCM must not spawn FFmpeg'))
    session = NativePcmSession(4)
    session.start()
    pcm = struct.pack('<5h', -32768, -1234, 0, 1234, 32767)
    session.push_chunk(pcm)
    assert session.read_pcm(len(pcm)) == pcm
    assert session.snapshot()['receiving']
    session.stop()
    assert not session.running


def test_stalled_capture_reports_no_signal_then_expires():
    now = [10.0]
    session = NativePcmSession(4, clock=lambda: now[0])
    session.start()
    session.push_chunk(b'\x01\x00' * 4800)
    now[0] += 1
    assert not session.snapshot()['receiving']
    assert not session.snapshot()['expired']
    now[0] += 2
    assert session.snapshot()['expired']


def test_fifo_is_bounded_and_rejects_partial_frames():
    session = NativePcmSession(4, max_buffer_bytes=10**9)
    session.start()
    for _ in range(20):
        session.push_chunk(b'\x00\x00' * 4800)
    assert session.snapshot()['buffer_bytes'] <= 38400
    with pytest.raises(ValueError):
        session.push_chunk(b'\x00')
    with pytest.raises(ValueError):
        session.push_chunk(b'\x00' * 9602)


def test_registry_releases_expired_native_owner_without_stopping_program():
    now = [10.0]
    registry = LiveMicRegistry(render_session_factory=lambda sid, **kw: NativePcmSession(sid, clock=lambda: now[0]))
    registry.start_render_transmission(4, {'id': 1, 'username': 'operator', 'role': 'admin'}, sample_rate=48000)
    registry.push_render_chunk(4, b'\x00\x00')
    assert registry.snapshot(4)['transmitting']
    now[0] += 4
    status = registry.snapshot(4)
    assert not status['transmitting']
    assert status['active_user'] is None
    assert registry.read_pcm(4, 100) == b'\x00' * 100


def test_native_pcm_reaches_isolated_worker_bridge_bit_exact(tmp_path):
    from app.engine.process_audio_bridge import ProcessAudioBridgeHost, ProcessAudioBridgeClient
    registry = LiveMicRegistry()
    pcm = struct.pack('<4h', -3000, -1000, 1000, 3000)
    host = ProcessAudioBridgeHost(tmp_path / 'audio.bridge', 4, live_mic_registry=registry)
    client = ProcessAudioBridgeClient(tmp_path / 'audio.bridge')
    try:
        registry.start_render_transmission(4, {'id': 1, 'username': 'operator', 'role': 'admin'}, sample_rate=48000)
        registry.push_render_chunk(4, pcm)
        host._pump_once()
        assert client.snapshot(4)['receiving']
        assert client.read_pcm(4, len(pcm)) == pcm
        registry.stop_render_transmission(4, user_id=1)
        host._pump_once()
        assert not client.snapshot(4)['transmitting']
        assert client.read_pcm(4, len(pcm)) == b'\x00' * len(pcm)
    finally:
        client.close()
        host.close()
        registry.reset()
