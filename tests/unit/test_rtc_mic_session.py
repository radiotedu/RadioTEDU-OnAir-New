import struct
import threading
from collections import deque
from pathlib import Path

from app.audio.mic_session import MicSession


def test_rtc_mic_session_module_exists():
    module_path = (
        Path(__file__).resolve().parents[2] / "app" / "audio" / "rtc_mic_session.py"
    )
    assert module_path.exists()
    source = module_path.read_text(encoding="utf-8")
    assert "class RtcMicSession" in source
    assert "def read_pcm" in source
    assert "def snapshot" in source


def test_rtc_mic_session_buffer_append_and_read():
    from app.audio.rtc_mic_session import RtcMicSession

    session = RtcMicSession(station_id=1, max_buffer_bytes=96000)
    session._running = True

    pcm = struct.pack("<4h", 1000, -1000, 2000, -2000)
    session._append_pcm(pcm)

    result = session.read_pcm(8)
    assert result == pcm


def test_rtc_mic_session_read_pcm_pads_with_silence():
    from app.audio.rtc_mic_session import RtcMicSession

    session = RtcMicSession(station_id=1, max_buffer_bytes=96000)
    session._running = True

    pcm = struct.pack("<2h", 500, -500)
    session._append_pcm(pcm)

    result = session.read_pcm(8)
    assert result[:4] == pcm
    assert result[4:] == b"\x00" * 4


def test_rtc_mic_session_buffer_overflow_drops_oldest():
    from app.audio.rtc_mic_session import RtcMicSession

    session = RtcMicSession(station_id=1, max_buffer_bytes=8)
    session._running = True

    chunk_a = struct.pack("<2h", 100, 200)
    chunk_b = struct.pack("<2h", 300, 400)
    chunk_c = struct.pack("<2h", 500, 600)

    session._append_pcm(chunk_a)
    session._append_pcm(chunk_b)
    session._append_pcm(chunk_c)

    result = session.read_pcm(8)
    assert result == chunk_b + chunk_c


def test_rtc_mic_session_snapshot_shape():
    from app.audio.rtc_mic_session import RtcMicSession

    session = RtcMicSession(station_id=7)
    snap = session.snapshot()
    assert snap["station_id"] == 7
    assert snap["running"] is False
    assert snap["receiving"] is False
    assert "buffer_bytes" in snap
    assert "level_db" in snap
    assert "peak_db" in snap
    assert "last_error" in snap
    assert "last_chunk_at" in snap


def test_rtc_mic_session_running_property_defaults_false():
    from app.audio.rtc_mic_session import RtcMicSession

    session = RtcMicSession(station_id=1)
    assert session.running is False


def test_rtc_mic_session_parity_with_mic_session():
    """Both session types must return identical bytes from read_pcm given identical PCM input."""
    from app.audio.rtc_mic_session import RtcMicSession

    pcm = struct.pack("<8h", 100, -100, 200, -200, 300, -300, 400, -400)

    # RtcMicSession path
    rtc = RtcMicSession(station_id=1, max_buffer_bytes=96000)
    rtc._running = True
    rtc._append_pcm(pcm)
    rtc_result = rtc.read_pcm(len(pcm))

    # MicSession path — bypass __init__ to avoid ffmpeg dependency
    # _append_pcm and read_pcm only depend on: _buffer, _buffer_bytes, _lock,
    # max_buffer_bytes, _level_db, _peak_db
    mic = MicSession.__new__(MicSession)
    mic.station_id = 1
    mic.max_buffer_bytes = 96000
    mic._buffer = deque()
    mic._buffer_bytes = 0
    mic._lock = threading.Lock()
    mic._level_db = -60.0
    mic._peak_db = -60.0
    mic._append_pcm(pcm)
    mic_result = mic.read_pcm(len(pcm))

    assert rtc_result == mic_result


def test_aiortc_available_returns_bool():
    from app.audio.rtc_mic_session import aiortc_available
    result = aiortc_available()
    assert isinstance(result, bool)
