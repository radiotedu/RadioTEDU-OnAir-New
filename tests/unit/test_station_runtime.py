import time
import threading

import app.audio.station_runtime as runtime_module
from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.station_runtime import StationRuntime


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self._running = True
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminated = True
        self._running = False

    def wait(self, timeout=None):
        self._running = False
        return 0

    def kill(self):
        self._running = False


class _FakePipe:
    def __init__(self):
        self.closed = False
        self.writes = []

    def close(self):
        self.closed = True

    def write(self, data):
        payload = bytes(data or b"")
        self.writes.append(payload)
        return len(payload)

    def read(self, _size=-1):
        return b""

    def flush(self):
        return None


class _FakeLiveMicRegistry:
    def __init__(self, *, transmitting: bool, active_user: dict | None = None, mic_pcm: bytes = b""):
        self.transmitting = bool(transmitting)
        self.active_user = active_user
        self.mic_pcm = bytes(mic_pcm)

    def snapshot(self, station_id: int):
        return {
            "station_id": int(station_id),
            "live_input_enabled": bool(self.transmitting),
            "transmitting": bool(self.transmitting),
            "active_user": self.active_user,
            "receiving": bool(self.transmitting),
            "level_db": -12.0,
            "peak_db": -6.0,
            "buffer_bytes": len(self.mic_pcm),
            "last_error": "",
        }

    def read_pcm(self, station_id: int, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        chunk = self.mic_pcm[:requested]
        if len(chunk) < requested:
            chunk += b"\x00" * (requested - len(chunk))
        return chunk


class _HealthySink:
    stdin = _FakePipe()

    def is_running(self):
        return True

    def health_snapshot(self):
        return {
            "process_running": True,
            "mount_healthy": True,
            "consecutive_probe_failures": 0,
        }


def _make_gst_missing_factory():
    launched = []
    ffmpeg_procs = []
    ffplay_procs = []

    def _factory(cmd, **kwargs):
        launched.append(cmd)
        if cmd[0] == "gst-launch-1.0":
            raise FileNotFoundError("gst-launch-1.0")
        proc = _FakeProcess()
        if cmd[0] == "ffmpeg.exe":
            ffmpeg_procs.append(proc)
        if cmd[0] == "ffplay.exe":
            ffplay_procs.append(proc)
        return proc

    return launched, ffmpeg_procs, ffplay_procs, _factory


def _make_cfg(
    input_uri: str = "C:/music/fallback.mp3",
    icecast_enabled: bool = True,
    local_output_enabled: bool = True,
    track_type: str = "music",
    crossfade_seconds: float = 0.0,
):
    return StationPipelineConfig(
        input_uri=input_uri,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=local_output_enabled,
        output_device_id="dev1",
        icecast_enabled=icecast_enabled,
        track_type=track_type,
        crossfade_seconds=crossfade_seconds,
    )


def test_runtime_start_stop_and_branch_health():
    launched = []
    fake_proc = _FakeProcess()

    def _factory(cmd):
        launched.append(cmd)
        return fake_proc

    runtime = StationRuntime(process_factory=_factory)
    cfg = _make_cfg()
    runtime.start(cfg)
    assert runtime.is_running() is True
    assert runtime.status()["active_input_uri"] == cfg.input_uri
    assert launched
    assert launched[0][0] == "gst-launch-1.0"
    assert "-e" in launched[0]

    runtime.set_branch_health("local", False)
    health = runtime.branch_health()
    assert health["icecast"] is True
    assert health["local"] is False

    runtime.stop()
    assert fake_proc.terminated is True
    assert runtime.is_running() is False
    health_after_stop = runtime.branch_health()
    assert health_after_stop["icecast"] is False
    assert health_after_stop["local"] is False


def test_runtime_does_not_report_silence_floor_as_live_program_audio():
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())
    runtime._backend = "ffmpeg"
    runtime._process = _FakeProcess()
    runtime._icecast_sink = _HealthySink()
    runtime._router.set_branch_health("icecast", True)
    runtime._last_program_pcm_monotonic = (
        time.monotonic() - runtime_module._PROGRAM_PCM_STALL_SECONDS - 1.0
    )

    status = runtime.status()

    assert status["program_running"] is True
    assert status["program_pcm_stalled"] is True
    assert status["output_feed_active"] is False
    assert status["branch_health"]["icecast"] is False


def test_runtime_tracks_decode_progress_when_remote_sink_is_unhealthy():
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())

    class RepeatingPipe:
        def read(self, _size=-1):
            return b"\x01\x00" * 128

        def close(self):
            return None

    class UnhealthySink:
        stdin = _FakePipe()

        def is_running(self):
            return True

        def health_snapshot(self):
            return {
                "process_running": True,
                "mount_healthy": False,
                "consecutive_probe_failures": 3,
            }

    producer = _FakeProcess()
    producer.stdout = RepeatingPipe()
    runtime._backend = "ffmpeg"
    runtime._process = producer
    runtime._icecast_sink = UnhealthySink()
    runtime._last_program_pcm_monotonic = time.monotonic() - 30.0

    worker = threading.Thread(
        target=runtime._icecast_pipe_loop,
        args=(producer, runtime._icecast_sink, runtime._playout_generation),
        daemon=True,
    )
    worker.start()
    time.sleep(0.02)
    runtime._icecast_pipe_stop.set()
    worker.join(timeout=1.0)

    assert time.monotonic() - runtime._last_program_pcm_monotonic < 1.0
    status = runtime.status()
    assert status["program_pcm_stalled"] is False
    assert status["branch_health"]["icecast"] is True
    assert status["delivery_health"]["icecast"] is False


def test_runtime_falls_back_to_ffmpeg_when_gst_missing():
    launched = []
    fake_proc = _FakeProcess()

    def _factory(cmd):
        if not launched:
            launched.append(cmd)
            raise FileNotFoundError("gst-launch-1.0")
        launched.append(cmd)
        return fake_proc

    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = None
    cfg = _make_cfg()
    runtime.start(cfg)
    assert runtime.is_running() is True
    assert launched[0][0] == "gst-launch-1.0"
    assert launched[1][0] == "ffmpeg.exe"
    health = runtime.branch_health()
    assert health["icecast"] is True
    assert health["local"] is False


def test_runtime_uses_ffplay_for_local_only_when_gst_missing():
    launched = []
    ffmpeg_proc = _FakeProcess()
    ffplay_proc = _FakeProcess()

    def _factory(cmd, **kwargs):
        if not launched:
            launched.append(cmd)
            raise FileNotFoundError("gst-launch-1.0")
        launched.append(cmd)
        if cmd[0] == "ffplay.exe":
            return ffplay_proc
        return ffmpeg_proc

    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"
    cfg = _make_cfg(icecast_enabled=False)
    runtime.start(cfg)
    assert runtime.is_running() is True
    assert launched[0][0] == "gst-launch-1.0"
    assert launched[1][0] == "ffplay.exe"
    assert launched[2][0] == "ffmpeg.exe"
    assert runtime._backend == "ffmpeg-local"
    health = runtime.branch_health()
    assert health["icecast"] is False
    assert health["local"] is True


def test_runtime_does_not_restart_for_identical_config():
    launched = []
    fake_proc = _FakeProcess()

    def _factory(cmd):
        launched.append(cmd)
        return fake_proc

    runtime = StationRuntime(process_factory=_factory)
    cfg = _make_cfg(input_uri="C:/music/a.mp3")
    runtime.start(cfg)
    runtime.start(cfg)

    assert runtime.is_running() is True
    assert len(launched) == 1


def test_runtime_restarts_when_input_changes():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(_make_cfg(input_uri="C:/music/a.mp3"))
    runtime.start(_make_cfg(input_uri="C:/music/b.mp3"))

    assert procs[0].terminated is True
    assert runtime.is_running() is True
    assert len(launched) == 2


def test_runtime_uses_crossfade_path_for_music_to_music():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert runtime._last_transition_mode == "crossfade"


def test_runtime_keeps_hard_cut_for_music_to_ads():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/ads/b.mp3",
            track_type="ads",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert runtime._last_transition_mode == "restart"
    assert sum("-filter_complex" in cmd for cmd in launched) == 0


def test_runtime_uses_short_crossfade_for_music_to_jingle():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/jingles/id.mp3",
            track_type="jingle",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert runtime._last_transition_mode == "crossfade"
    assert runtime._active_cfg.crossfade_seconds == 0.25


def test_runtime_keeps_hard_cut_for_ads_to_music():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(
        _make_cfg(
            input_uri="C:/ads/a.mp3",
            track_type="ads",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert runtime._last_transition_mode == "restart"
    assert sum("-filter_complex" in cmd for cmd in launched) == 0


def test_runtime_disables_crossfade_when_seconds_are_zero():
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    runtime = StationRuntime(process_factory=_factory)
    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=0.0,
            local_output_enabled=False,
        )
    )

    assert runtime._last_transition_mode == "restart"
    assert sum("-filter_complex" in cmd for cmd in launched) == 0


def test_runtime_uses_ffmpeg_transition_for_music_to_music_when_supported(monkeypatch):
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]
    clock = {"value": 100.0}

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    clock["value"] = 105.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    ffmpeg_cmds = [cmd for cmd in launched if cmd[0] == "ffmpeg.exe"]
    transition_cmd = next(cmd for cmd in ffmpeg_cmds if "-filter_complex" in cmd)

    assert launched[0][0] == "ffmpeg.exe"
    # Icecast authentication is handled by the in-memory source transport,
    # so FFmpeg owns only the original and transition PCM producers.
    assert len(ffmpeg_cmds) == 2
    assert "-ss" in transition_cmd
    assert "5.000" in " ".join(transition_cmd)
    assert procs[0].terminated is True
    assert procs[1].terminated is False
    assert runtime.is_running() is True
    assert runtime._last_transition_mode == "crossfade"


def test_runtime_falls_back_to_restart_when_transition_setup_fails(monkeypatch):
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]
    clock = {"value": 10.0}
    started = {"count": 0}

    def _factory(cmd):
        launched.append(cmd)
        if "-filter_complex" in cmd:
            raise RuntimeError("transition failed")
        proc = procs[started["count"]]
        started["count"] += 1
        return proc

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    clock["value"] = 14.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert [cmd[0] for cmd in launched] == ["ffmpeg.exe"] * 3
    assert "-filter_complex" in launched[1]
    assert procs[0].terminated is True
    assert procs[1].terminated is False
    assert runtime.is_running() is True
    assert runtime._last_transition_mode == "restart"


def test_runtime_falls_back_to_restart_when_local_transition_backend_is_missing(monkeypatch):
    launched = []
    procs = [_FakeProcess(), _FakeProcess()]
    clock = {"value": 1.0}

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = None

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            icecast_enabled=False,
            local_output_enabled=True,
        )
    )
    clock["value"] = 2.5
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            icecast_enabled=False,
            local_output_enabled=True,
        )
    )

    assert [cmd[0] for cmd in launched] == ["gst-launch-1.0", "gst-launch-1.0"]
    assert runtime._last_transition_mode == "restart"


def test_runtime_does_not_chain_crossfade_during_active_transition(monkeypatch):
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess(), _FakeProcess()]
    clock = {"value": 20.0}

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=_factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    clock["value"] = 21.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    clock["value"] = 22.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/c.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )

    assert [cmd[0] for cmd in launched] == ["ffmpeg.exe"] * 3
    assert sum("-filter_complex" in cmd for cmd in launched) == 1
    assert "-filter_complex" not in launched[-1]
    assert runtime._last_transition_mode == "restart"


def test_runtime_local_only_start_uses_raw_pcm_ffmpeg_producer_when_gst_is_missing():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert len(ffmpeg_procs) == 1
    ffplay_joined = " ".join(next(cmd for cmd in launched if cmd[0] == "ffplay.exe"))
    joined = " ".join(next(cmd for cmd in launched if cmd[0] == "ffmpeg.exe"))
    assert "-infbuf" in ffplay_joined
    assert "C:/music/a.mp3" in joined
    assert "-readrate 1" in joined
    assert "-readrate_initial_burst 10.000" in joined
    assert "-readrate_catchup 2.000" in joined
    assert "pipe:1" in joined
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ac 2" in joined


def test_runtime_local_only_track_change_reuses_persistent_sink_when_gst_is_missing():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert len(ffmpeg_procs) == 2
    assert ffmpeg_procs[0].terminated is True
    assert ffplay_procs[0].terminated is False


def test_runtime_local_only_crossfade_reuses_persistent_sink_when_gst_is_missing(
    monkeypatch,
):
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()
    clock = {"value": 50.0}

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )
    clock["value"] = 53.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )

    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert [cmd[0] for cmd in launched].count("ffmpeg.exe") == 2
    joined = " ".join(launched[-1])
    assert "-readrate_initial_burst 10.000" in joined
    assert "-readrate_catchup 2.000" in joined
    assert ffmpeg_procs[0].terminated is True
    assert ffplay_procs[0].terminated is False
    assert runtime._last_transition_mode == "crossfade"


def test_runtime_recreates_persistent_sink_after_local_sink_dies_when_gst_is_missing():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )
    ffplay_procs[0]._running = False
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 2
    assert len(ffmpeg_procs) == 2
    assert ffplay_procs[1].terminated is False


def test_runtime_terminates_orphaned_local_producer_before_restart_after_primary_exit():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=True,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    ffmpeg_procs[0]._running = False

    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=True,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert len(ffmpeg_procs) == 4
    assert ffmpeg_procs[1].terminated is True
    assert ffmpeg_procs[0].terminated is False


def test_runtime_icecast_only_track_change_reuses_persistent_sink_when_gst_is_missing():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = None

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=True,
            local_output_enabled=False,
            crossfade_seconds=0.0,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=True,
            local_output_enabled=False,
            crossfade_seconds=0.0,
        )
    )

    sink_cmds = [cmd for cmd in launched if "icecast://" in " ".join(cmd) and "pipe:0" in " ".join(cmd)]
    producer_cmds = [cmd for cmd in launched if "pipe:1" in " ".join(cmd)]

    assert len(ffplay_procs) == 0
    assert len(sink_cmds) == 0
    assert len(producer_cmds) == 2
    assert all("-readrate_initial_burst 10.000" in " ".join(cmd) for cmd in producer_cmds)
    assert all("-readrate_catchup 2.000" in " ".join(cmd) for cmd in producer_cmds)
    assert len(ffmpeg_procs) == 2
    assert ffmpeg_procs[0].terminated is True


def test_runtime_icecast_only_crossfade_reuses_persistent_sink_when_gst_is_missing(
    monkeypatch,
):
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()
    clock = {"value": 80.0}

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = None

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=True,
            local_output_enabled=False,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )
    clock["value"] = 83.0
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=True,
            local_output_enabled=False,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )

    sink_cmds = [cmd for cmd in launched if "icecast://" in " ".join(cmd) and "pipe:0" in " ".join(cmd)]
    producer_cmds = [cmd for cmd in launched if "pipe:1" in " ".join(cmd)]
    crossfade_cmds = [cmd for cmd in producer_cmds if "-filter_complex" in cmd]

    assert len(ffplay_procs) == 0
    assert len(sink_cmds) == 0
    assert len(producer_cmds) == 2
    assert len(crossfade_cmds) == 1
    assert "-readrate_initial_burst 10.000" in " ".join(crossfade_cmds[0])
    assert "-readrate_catchup 2.000" in " ".join(crossfade_cmds[0])
    assert len(ffmpeg_procs) == 2
    assert ffmpeg_procs[0].terminated is True
    assert runtime._last_transition_mode == "crossfade"


def test_runtime_icecast_and_local_track_change_reuses_both_persistent_sinks_when_gst_is_missing():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()

    runtime = StationRuntime(process_factory=factory)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=True,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=True,
            local_output_enabled=True,
            crossfade_seconds=0.0,
        )
    )

    sink_cmds = [cmd for cmd in launched if "icecast://" in " ".join(cmd) and "pipe:0" in " ".join(cmd)]
    producer_cmds = [cmd for cmd in launched if "pipe:1" in " ".join(cmd)]

    assert len(sink_cmds) == 0
    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert len(producer_cmds) == 4
    assert len(ffmpeg_procs) == 4
    assert ffmpeg_procs[0].terminated is True
    assert ffmpeg_procs[1].terminated is True
    assert ffplay_procs[0].terminated is False


def test_runtime_live_mix_reuses_sink_and_disables_crossfade_for_music_to_music():
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()
    live_registry = _FakeLiveMicRegistry(
        transmitting=True,
        active_user={"id": 5, "username": "dj", "role": "dj"},
        mic_pcm=int(600).to_bytes(2, byteorder="little", signed=True) * 4,
    )

    runtime = StationRuntime(
        process_factory=factory,
        station_id=1,
        live_mic_registry=live_registry,
        live_settings_provider=lambda station_id: {
            "program_music_mode": "duck",
            "mic_gain": 1.0,
            "music_gain": 1.0,
            "duck_level": 0.25,
        },
    )
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.ffplay_bin = "ffplay.exe"

    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )
    runtime.start(
        _make_cfg(
            input_uri="C:/music/b.mp3",
            icecast_enabled=False,
            local_output_enabled=True,
            track_type="music",
            crossfade_seconds=3.0,
        )
    )

    status = runtime.status()
    assert [cmd[0] for cmd in launched].count("ffplay.exe") == 1
    assert len(ffmpeg_procs) == 2
    assert ffmpeg_procs[0].terminated is True
    assert ffplay_procs[0].terminated is False
    assert runtime._last_transition_mode == "restart"
    assert status["backend"] == "live-mix"
    assert status["live_mic_active"] is True
    assert status["live_mic_user"]["username"] == "dj"
    assert status["program_music_mode"] == "duck"


def test_stop_reaps_owned_process_when_active_reference_was_lost():
    proc = _FakeProcess()
    runtime = StationRuntime(process_factory=lambda _cmd, **_kwargs: proc)

    spawned = runtime._spawn_process(["ffmpeg"])
    runtime._process = None
    runtime._icecast_sink = None
    runtime.stop()

    assert spawned is proc
    assert proc.terminated is True
    assert runtime._owned_processes == []
