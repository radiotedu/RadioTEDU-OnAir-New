import time

import app.audio.station_runtime as runtime_module
from app.audio.live_mic_registry import LiveMicRegistry
from app.audio.station_runtime import StationRuntime
from app.db import get_connection, init_db
from app.engine.runtime_registry import StationRuntimeRegistry
from app.repositories.station_output_repo import StationOutputRepository


class _WritablePipe:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        payload = bytes(data or b"")
        self.chunks.append(payload)
        return len(payload)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _ReadablePipe:
    def __init__(self, chunks):
        self._chunks = [bytes(chunk) for chunk in chunks]
        self.closed = False

    def read(self, size=-1):
        if self.closed or not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size is not None and size >= 0 and len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, *, stdout_chunks=None):
        self.stdin = _WritablePipe()
        self.stdout = _ReadablePipe(stdout_chunks or [])
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.terminated or self.killed:
            return 0
        if self.stdout.closed:
            return 0
        if not self.stdout._chunks:
            return 0
        return None

    def terminate(self):
        self.terminated = True
        self.stdout.close()

    def wait(self, timeout=None):
        self.terminated = True
        self.stdout.close()
        return 0

    def kill(self):
        self.killed = True
        self.stdout.close()


class _FakeRenderSession:
    def __init__(self, station_id: int, **kwargs):
        self.station_id = int(station_id)
        self.kwargs = dict(kwargs)
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def push_chunk(self, chunk: bytes) -> None:
        return None

    def read_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        return (int(600).to_bytes(2, byteorder="little", signed=True) * (requested // 2 + 1))[:requested]

    def snapshot(self) -> dict:
        return {
            "station_id": self.station_id,
            "running": self.running,
            "receiving": True,
            "buffer_bytes": 4096,
            "level_db": -10.0,
            "peak_db": -4.0,
            "last_error": "",
        }


def _wait_for(predicate, *, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_live_render_start_promotes_running_station_to_live_mix(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="dev1",
        icecast_enabled=False,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        output_gain_db=0.0,
    )
    conn.close()

    launched = []
    local_sink = _FakeProcess(stdout_chunks=[b"keepalive"])
    music_frames = int(1000).to_bytes(2, byteorder="little", signed=True)
    music_frames = (music_frames + music_frames) * 2
    music_producer = _FakeProcess(stdout_chunks=[music_frames])
    steady_state = _FakeProcess(stdout_chunks=[b"ignored"])
    clock = {"value": 100.0}
    render_registry = LiveMicRegistry(
        render_session_factory=lambda station_id, **kwargs: _FakeRenderSession(station_id, **kwargs)
    )

    def _factory(cmd, **kwargs):
        launched.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[0] == "gst-launch-1.0":
            return steady_state
        if "-window_title" in cmd and "pipe:0" in cmd:
            return local_sink
        if cmd[0] == "ffmpeg.exe" and "pipe:1" in joined:
            return music_producer
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    registry = StationRuntimeRegistry(
        runtime_factory=lambda station_id: _build_runtime(station_id, _factory),
        live_mic_registry=render_registry,
    )

    started = registry.start_station(1, input_uri="C:/music/live.mp3")
    assert started["backend"] != "live-mix"

    clock["value"] = 108.0
    render_registry.start_render_transmission(
        1,
        {"id": 9, "username": "dj-live", "role": "dj"},
        source_name="OmniVoice",
        input_format="s16le",
        sample_rate=24000,
        channels=1,
    )

    assert _wait_for(lambda: bool(local_sink.stdin.chunks))
    status = registry.status(1)
    assert status["backend"] == "live-mix"
    assert status["live_mic_active"] is True
    assert status["live_mic_user"]["username"] == "dj-live"
    producer_cmd = next(cmd for cmd in launched if cmd[0] == "ffmpeg.exe" and "pipe:1" in " ".join(cmd))
    assert "-ss" in producer_cmd
    assert "8.000" in " ".join(producer_cmd)


def _build_runtime(station_id: int, process_factory):
    runtime = StationRuntime(
        process_factory=process_factory,
        station_id=station_id,
    )
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"
    return runtime
