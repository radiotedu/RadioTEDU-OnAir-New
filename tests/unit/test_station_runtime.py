from dataclasses import replace
import threading
import time

import pytest

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
        self.peek_data = bytes(4096)

    def close(self):
        self.closed = True

    def write(self, data):
        payload = bytes(data or b"")
        self.writes.append(payload)
        return len(payload)

    def read(self, _size=-1):
        return b""

    def peek(self, size=-1):
        return self.peek_data if size < 0 else self.peek_data[:size]

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


def _allow_fake_transition_paths(monkeypatch):
    real_isfile = runtime_module.os.path.isfile
    monkeypatch.setattr(
        runtime_module.os.path,
        "isfile",
        lambda path: str(path).startswith("C:/") or real_isfile(path),
    )


def _advance_fake_clock_on_sleep(monkeypatch, clock):
    real_sleep = time.sleep

    def sleep(seconds):
        clock["value"] += seconds
        real_sleep(0.001)  # Yield so connector/encoder test threads can run.

    monkeypatch.setattr(runtime_module.time, "sleep", sleep)


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


def _mount_telemetry(*, saturation=None):
    health = {
        "process_running": True,
        "mount_healthy": True,
        "writer_running": True,
        "writer_failed": False,
        "writer_backpressured": False,
        "writer_backpressure_age_seconds": 0.0,
        "queued_pcm_seconds": 3.0,
        "queued_pcm_chunks": 140,
        "pcm_queue_capacity_chunks": 1024,
        "pcm_dispatcher_running": True,
        "pcm_dispatch_backpressured": False,
        "pcm_dispatch_backpressure_age_seconds": 0.0,
        "queued_dispatch_pcm_seconds": 2.0,
        "queued_dispatch_pcm_chunks": 94,
        "pcm_dispatch_queue_capacity_chunks": 512,
        "network_writer_running": True,
        "network_failed": False,
        "last_write_age_seconds": 0.2,
        "last_network_write_age_seconds": 0.2,
    }
    if saturation == "pcm":
        health.update(
            {
                "writer_backpressured": True,
                "writer_backpressure_age_seconds": 31.0,
                "queued_pcm_seconds": 20.9,
                "queued_pcm_chunks": 1000,
            }
        )
    elif saturation == "dispatch":
        health.update(
            {
                "pcm_dispatch_backpressured": True,
                "pcm_dispatch_backpressure_age_seconds": 31.0,
                "queued_dispatch_pcm_seconds": 10.6,
                "queued_dispatch_pcm_chunks": 500,
            }
        )
    return health


class _QueueTelemetrySink:
    def __init__(self, *, saturation=None):
        self._health = _mount_telemetry(saturation=saturation)

    def is_running(self):
        return True

    def health_snapshot(self):
        return dict(self._health)


@pytest.mark.parametrize("branch", ["icecast", "icecast:/station-low"])
@pytest.mark.parametrize("saturation", ["pcm", "dispatch"])
def test_delivery_health_rejects_sustained_saturated_mount_with_fresh_writes(
    branch, saturation
):
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())
    runtime._backend = "ffmpeg"
    runtime._process = _FakeProcess()
    runtime._last_program_pcm_monotonic = time.monotonic()
    runtime._router.set_branch_health("icecast", True)
    runtime._icecast_sink = _QueueTelemetrySink(
        saturation=saturation if branch == "icecast" else None
    )

    if branch == "icecast:/station-low":
        low_cfg = replace(
            _make_cfg(),
            icecast_mount="/station-low",
            stream_codec_profile="aac_low_96",
            stream_bitrate_kbps=96,
        )
        runtime._extra_icecast_configs[branch] = low_cfg
        runtime._extra_icecast_sinks[branch] = _QueueTelemetrySink(
            saturation=saturation
        )
        runtime._router.set_branch_health(branch, True)

    status = runtime.status()

    assert status["delivery_health"][branch] is False


def test_recover_outputs_reconnects_sinks_without_restarting_programme(monkeypatch):
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())
    cfg = _make_cfg(local_output_enabled=False)
    runtime._active_cfg = cfg
    runtime._active_started_monotonic = time.monotonic() - 12.0
    calls = []

    class Sink:
        def __init__(self):
            self.accepted = []

        def stop(self, **_kwargs):
            calls.append("stop-primary")

        def write_pcm(self, chunk):
            self.accepted.append(chunk)
            return True

        def is_running(self):
            return True

    primary = Sink()
    runtime._icecast_sink = primary

    def during_release(seconds):
        calls.append(("release", seconds))
        targets = runtime._icecast_output_targets()
        assert targets == [("icecast", primary)]
        assert runtime._write_pcm_chunk_to_targets(
            b"pcm-during-reconnect", targets, program_data=False
        ) is True

    monkeypatch.setattr(
        "app.audio.station_runtime.time.sleep",
        during_release,
    )
    monkeypatch.setattr(
        runtime,
        "_ensure_icecast_sink",
        lambda _cfg, **_kwargs: calls.append("start-primary") or True,
    )
    monkeypatch.setattr(
        runtime,
        "_release_disabled_sinks",
        lambda _cfg: calls.append("release-disabled"),
    )
    monkeypatch.setattr(runtime, "status", lambda: {"running": True})

    assert runtime.recover_outputs() == {"running": True}
    assert calls == [
        "stop-primary",
        ("release", 3.0),
        "start-primary",
        "release-disabled",
    ]
    assert primary.accepted == [b"pcm-during-reconnect"]


def test_recover_outputs_keeps_each_mount_in_fanout_during_release(monkeypatch):
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())
    cfg = replace(
        _make_cfg(local_output_enabled=False),
        extra_icecast_outputs=(
            {"enabled": True, "icecast_mount": "/backup"},
        ),
    )
    runtime._active_cfg = cfg
    calls = []

    class Sink:
        def stop(self, **_kwargs):
            calls.append("stop")

        def ensure_started(self, _cfg, **_kwargs):
            calls.append("start-extra")

        def is_running(self):
            return True

        def write_pcm(self, _chunk):
            return True

    primary = Sink()
    backup = Sink()
    runtime._icecast_sink = primary
    runtime._extra_icecast_sinks = {"icecast:/backup": backup}

    def during_release(seconds):
        calls.append(("release", seconds))
        assert runtime._icecast_output_targets() == [
            ("icecast", primary),
            ("icecast:/backup", backup),
        ]

    monkeypatch.setattr("app.audio.station_runtime.time.sleep", during_release)
    monkeypatch.setattr(runtime, "_ensure_icecast_sink", lambda *_a, **_kw: True)
    monkeypatch.setattr(runtime, "_release_disabled_sinks", lambda _cfg: None)
    monkeypatch.setattr(runtime, "status", lambda: {"running": True})

    assert runtime.recover_outputs() == {"running": True}
    assert runtime._icecast_output_targets() == [
        ("icecast", primary),
        ("icecast:/backup", backup),
    ]
    assert calls.count(("release", 3.0)) == 2
    assert calls.count("start-extra") == 1


def test_recover_primary_output_uses_long_release_without_stopping_programme(monkeypatch):
    runtime = StationRuntime(process_factory=lambda _cmd: _FakeProcess())
    cfg = _make_cfg(local_output_enabled=False)
    runtime._active_cfg = cfg
    calls = []

    class Sink:
        def stop(self, **_kwargs):
            calls.append("stop-primary")

    runtime._icecast_sink = Sink()
    monkeypatch.setattr(
        "app.audio.station_runtime.time.sleep",
        lambda seconds: calls.append(("release", seconds)),
    )
    monkeypatch.setattr(
        runtime,
        "_ensure_icecast_sink",
        lambda _cfg, **_kwargs: calls.append("start-primary") or True,
    )
    monkeypatch.setattr(runtime, "status", lambda: {"running": True})

    assert runtime.recover_primary_output() == {"running": True}
    assert calls == ["stop-primary", ("release", 12.0), "start-primary"]


def test_recover_output_branch_reconnects_only_the_named_extra_mount(monkeypatch):
    cfg = replace(
        _make_cfg(local_output_enabled=False),
        extra_icecast_outputs=(
            {"enabled": True, "icecast_mount": "/backup"},
            {"enabled": True, "icecast_mount": "/other"},
        ),
    )
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._active_cfg = cfg
    calls = []

    class Sink:
        def __init__(self, name):
            self.name = name

        def stop(self, **kwargs):
            calls.append(("stop", self.name, kwargs.get("preserve_pcm")))

        def ensure_started(self, output_cfg, **kwargs):
            calls.append(
                (
                    "start",
                    self.name,
                    output_cfg.icecast_mount,
                    kwargs.get("preserve_pcm"),
                )
            )

        def is_running(self):
            return True

        def write_pcm(self, _chunk):
            return True

    primary = Sink("primary")
    backup = Sink("backup")
    other = Sink("other")
    runtime._icecast_sink = primary
    runtime._extra_icecast_sinks = {
        "icecast:/backup": backup,
        "icecast:/other": other,
    }
    monkeypatch.setattr(
        "app.audio.station_runtime.time.sleep",
        lambda seconds: calls.append(("release", seconds)),
    )
    monkeypatch.setattr(runtime, "status", lambda: {"running": True})

    assert runtime.recover_output_branch("icecast:/backup") == {"running": True}

    assert calls == [
        ("stop", "backup", True),
        ("release", runtime_module._ORIGIN_SOURCE_RELEASE_SECONDS),
        ("start", "backup", "/backup", True),
    ]
    assert runtime._icecast_sink is primary
    assert runtime._extra_icecast_sinks["icecast:/other"] is other


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


def test_forced_restart_replaces_same_source_producer_and_preserves_sink_pcm(monkeypatch):
    processes = []
    ensured = []
    sink = _HealthySink()

    def spawn(cfg, *, start_offset_seconds=0.0):
        process = _FakeProcess()
        processes.append(process)
        return process

    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime._icecast_sink = sink
    monkeypatch.setattr(
        runtime,
        "_ensure_icecast_sink",
        lambda _cfg, *, preserve_pcm=False: ensured.append(bool(preserve_pcm)) or True,
    )
    monkeypatch.setattr(
        runtime,
        "_ensure_extra_icecast_sinks",
        lambda _cfg, *, preserve_pcm=False: {},
    )
    monkeypatch.setattr(runtime, "_spawn_icecast_pcm_producer", spawn)
    monkeypatch.setattr(runtime, "_start_icecast_pipe_worker", lambda *_args: None)
    monkeypatch.setattr(runtime, "_start_silence_floor_worker", lambda: None)
    monkeypatch.setattr(runtime, "_release_disabled_sinks", lambda _cfg: None)

    cfg = replace(
        _make_cfg(
            input_uri="C:/music/same-song.flac",
            local_output_enabled=False,
        ),
        stream_title="Same Song",
        stream_artist="Same Artist",
    )
    runtime.start(cfg)
    original_generation = runtime._playout_generation
    original_process = runtime._process
    original_signature = runtime._active_signature

    runtime._program_fanout_inflight = 1
    with pytest.raises(RuntimeError, match="forced producer restart precondition"):
        runtime.start(
            cfg,
            force_restart=True,
            expected_active_input_uri=cfg.input_uri,
        )
    assert runtime._process is original_process
    runtime._program_fanout_inflight = 0
    runtime._program_fanout_started_monotonic = None

    runtime.start(
        cfg,
        force_restart=True,
        expected_active_input_uri=cfg.input_uri,
    )

    assert original_process is processes[0]
    assert original_process.terminated is True
    assert runtime._process is processes[1]
    assert runtime._playout_generation > original_generation
    assert runtime._active_signature == original_signature
    assert runtime.status()["active_input_uri"] == cfg.input_uri
    assert runtime._icecast_sink is sink
    assert ensured == [False, True]


def test_producer_eof_waits_for_each_configured_output_fifo_to_drain():
    class WatermarkSink:
        def __init__(self, *, accepted=100, drained=100):
            self.accepted = accepted
            self.drained = drained

        def pcm_drain_watermark(self):
            return {
                "epoch": 2,
                "accepted_bytes": self.accepted,
                "drained_bytes": self.drained,
            }

    class ExitedProducer:
        returncode = 0

        def poll(self):
            return self.returncode

    cfg = replace(
        _make_cfg(local_output_enabled=False),
        extra_icecast_outputs=({"enabled": True, "icecast_mount": "/backup"},),
    )
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._active_cfg = cfg
    runtime._playout_generation = 4
    runtime._icecast_sink = WatermarkSink()
    backup = WatermarkSink(drained=0)
    runtime._extra_icecast_sinks["icecast:/backup"] = backup

    assert runtime._required_icecast_branches() == {
        "icecast",
        "icecast:/backup",
    }
    runtime._record_producer_exit(ExitedProducer(), 4, pcm_accepted=True)

    assert runtime._producer_exit_drain_state(current=True) == (False, True)
    backup.drained = backup.accepted
    assert runtime._producer_exit_drain_state(current=True) == (True, False)


def test_missing_configured_primary_prevents_backup_only_eof():
    class WatermarkSink:
        def pcm_drain_watermark(self):
            return {"epoch": 1, "accepted_bytes": 12, "drained_bytes": 12}

    class ExitedProducer:
        returncode = 0

        def poll(self):
            return self.returncode

    cfg = replace(
        _make_cfg(local_output_enabled=False),
        extra_icecast_outputs=({"enabled": True, "icecast_mount": "/backup"},),
    )
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._active_cfg = cfg
    runtime._playout_generation = 9
    runtime._icecast_sink = None
    runtime._extra_icecast_sinks["icecast:/backup"] = WatermarkSink()

    runtime._record_producer_exit(ExitedProducer(), 9, pcm_accepted=False)

    assert "icecast" in runtime._required_icecast_branches()
    assert runtime._producer_exit_pcm_accepted is False
    assert runtime._producer_exit_drain_state(current=True) == (False, False)


def test_reconnecting_sink_admission_counts_even_when_sink_process_is_down():
    class ReconnectingSink:
        def __init__(self):
            self.accepted = 0
            self.drained = 0

        def write_pcm(self, chunk):
            self.accepted += len(chunk)
            return True

        def is_running(self):
            return False

        def pcm_drain_watermark(self):
            return {
                "epoch": 3,
                "accepted_bytes": self.accepted,
                "drained_bytes": self.drained,
            }

    class ExitedProducer:
        returncode = 0

        def poll(self):
            return self.returncode

    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._active_cfg = _make_cfg(local_output_enabled=False)
    runtime._playout_generation = 6
    sink = ReconnectingSink()
    runtime._icecast_sink = sink

    accepted = runtime._write_pcm_chunk_to_targets(
        b"frame-held-for-reconnect",
        [("icecast", sink)],
        program_data=False,
        generation=6,
        required_branches={"icecast"},
    )

    assert accepted is True
    assert runtime._router.is_output_active("icecast") is False
    runtime._record_producer_exit(ExitedProducer(), 6, pcm_accepted=accepted)
    assert runtime._producer_exit_pcm_accepted is True
    assert runtime._producer_exit_drain_state(current=True) == (False, True)

    sink.drained = sink.accepted
    assert runtime._producer_exit_drain_state(current=True) == (True, False)


def test_fanout_backpressure_is_reported_and_not_misclassified_as_decoder_stall():
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._backend = "ffmpeg"
    runtime._process = _FakeProcess()
    runtime._icecast_sink = _HealthySink()
    runtime._router.set_branch_health("icecast", True)
    runtime._last_program_pcm_monotonic = time.monotonic() - 30.0
    runtime._program_fanout_inflight = 1
    runtime._program_fanout_started_monotonic = time.monotonic() - 1.0

    status = runtime.status()

    assert status["program_pcm_stalled"] is False
    assert status["program_fanout_inflight"] is True
    assert status["program_fanout_blocked"] is True


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


def test_runtime_uses_crossfade_path_for_music_to_music(monkeypatch):
    _allow_fake_transition_paths(monkeypatch)
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


def test_runtime_uses_short_crossfade_for_music_to_jingle(monkeypatch):
    _allow_fake_transition_paths(monkeypatch)
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
    _allow_fake_transition_paths(monkeypatch)
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess()]
    clock = {"value": 100.0}

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    _advance_fake_clock_on_sleep(monkeypatch, clock)
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
    # Connector retries use the same fake clock in background threads. Pin
    # this command-builder input instead of depending on their scheduling.
    monkeypatch.setattr(runtime, "_current_offset_seconds", lambda: 5.0)
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
    transition_offset = float(transition_cmd[transition_cmd.index("-ss") + 1])
    assert 0.0 < transition_offset <= 5.0
    assert procs[0].terminated is True
    assert procs[1].terminated is False
    assert runtime.is_running() is True
    assert runtime._last_transition_mode == "crossfade"


def test_current_program_offset_uses_monotonic_time_and_never_goes_negative(monkeypatch):
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: _FakeProcess())
    clock = {"value": 105.0}
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    assert runtime._current_offset_seconds() == 0.0
    runtime._active_started_monotonic = 100.0
    assert runtime._current_offset_seconds() == 5.0
    clock["value"] = 99.0
    assert runtime._current_offset_seconds() == 0.0


def test_runtime_defers_failed_transition_without_killing_current_source(monkeypatch):
    _allow_fake_transition_paths(monkeypatch)
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
    with pytest.raises(RuntimeError, match="crossfade deferred"):
        runtime.start(
            _make_cfg(
                input_uri="C:/music/b.mp3",
                track_type="music",
                crossfade_seconds=3.0,
                local_output_enabled=False,
            )
        )

    assert [cmd[0] for cmd in launched] == ["ffmpeg.exe"] * 2
    assert "-filter_complex" in launched[1]
    assert procs[0].terminated is False
    assert runtime.is_running() is True
    assert runtime._last_transition_mode == "deferred"


def test_runtime_defers_crossfade_until_decoder_has_buffered_pcm(monkeypatch):
    _allow_fake_transition_paths(monkeypatch)
    monkeypatch.setattr(runtime_module, "_CROSSFADE_PREWARM_SECONDS", 0.03)
    procs = [_FakeProcess(), _FakeProcess()]
    procs[1].stdout.peek_data = bytes(runtime_module._LIVE_MIX_CHUNK_BYTES - 1)

    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: procs.pop(0))
    runtime.ffmpeg_bin = "ffmpeg.exe"
    runtime.start(
        _make_cfg(
            input_uri="C:/music/a.mp3",
            track_type="music",
            crossfade_seconds=3.0,
            local_output_enabled=False,
        )
    )
    current_process = runtime._process

    with pytest.raises(RuntimeError, match="crossfade deferred"):
        runtime.start(
            _make_cfg(
                input_uri="C:/music/b.mp3",
                track_type="music",
                crossfade_seconds=3.0,
                local_output_enabled=False,
            )
        )

    assert current_process is not None
    assert current_process.terminated is False
    assert runtime._last_transition_mode == "deferred"


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
    _allow_fake_transition_paths(monkeypatch)
    launched = []
    procs = [_FakeProcess(), _FakeProcess(), _FakeProcess(), _FakeProcess()]
    clock = {"value": 20.0}

    def _factory(cmd):
        launched.append(cmd)
        return procs[len(launched) - 1]

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    _advance_fake_clock_on_sleep(monkeypatch, clock)
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
    _allow_fake_transition_paths(monkeypatch)
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()
    clock = {"value": 50.0}

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    _advance_fake_clock_on_sleep(monkeypatch, clock)
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
    _allow_fake_transition_paths(monkeypatch)
    launched, ffmpeg_procs, ffplay_procs, factory = _make_gst_missing_factory()
    clock = {"value": 80.0}

    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["value"])
    _advance_fake_clock_on_sleep(monkeypatch, clock)
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


def test_primary_source_keeps_listener_verification_outside_reconnect_loop(monkeypatch):
    captured = {}

    class Sink:
        def __init__(self, *_args, **kwargs):
            captured.update(kwargs)

        def ensure_started(self, cfg, **_kwargs):
            captured["cfg"] = cfg

    monkeypatch.setattr(runtime_module, "IcecastAudioSink", Sink)
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    cfg = _make_cfg(local_output_enabled=False)
    assert runtime._ensure_icecast_sink(cfg)
    assert captured["mount_probe"] is None
    assert captured["decouple_input_backpressure"] is True
    assert captured["probe_failure_threshold"] == 2
    assert captured["cfg"] is cfg


def test_extra_source_keeps_listener_verification_outside_reconnect_loop(monkeypatch):
    captured = {}

    class Sink:
        def __init__(self, *_args, **kwargs):
            captured.update(kwargs)

        def ensure_started(self, _cfg, **_kwargs):
            pass

        def is_running(self):
            return True

    monkeypatch.setattr(runtime_module, "IcecastAudioSink", Sink)
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime.ffmpeg_bin = "ffmpeg.exe"
    cfg = replace(
        _make_cfg(local_output_enabled=False),
        extra_icecast_outputs=({"enabled": True, "icecast_mount": "/backup"},),
    )

    assert runtime._ensure_extra_icecast_sinks(cfg) == {"icecast:/backup": True}
    assert captured["mount_probe"] is None
    assert captured["decouple_input_backpressure"] is True
    assert captured["probe_failure_threshold"] == 2
