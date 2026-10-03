from types import SimpleNamespace
import threading
import time

import pytest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.station_runtime import StationRuntime
from app.engine.station_worker import StationWorker


def finishing_runtime():
    runtime = StationRuntime(process_factory=lambda *_args, **_kwargs: None)
    runtime._backend = "ffmpeg"
    runtime._active_cfg = StationPipelineConfig(
        input_uri="E:/Ads/PowerAPP.mp3", icecast_host="127.0.0.1",
        icecast_port=8000, icecast_mount="/radio", icecast_user="source",
        icecast_password="test", local_output_enabled=False, output_device_id="",
    )
    runtime._process = SimpleNamespace(poll=lambda: 0)
    runtime._icecast_pipe_process = runtime._process
    runtime._icecast_pipe_thread = SimpleNamespace(is_alive=lambda: True)
    runtime._playout_generation = 3
    runtime._icecast_pipe_generation = 3
    runtime._last_program_pcm_monotonic = time.monotonic()
    return runtime


def test_successful_decoder_exit_waits_for_its_current_pcm_pipe():
    runtime = finishing_runtime()
    status = runtime.status()
    assert status["program_running"] is False
    assert status["producer_finalizing"] is True
    assert status["producer_eof"] is False
    assert runtime.completed_producer_generation() is None


@pytest.mark.parametrize("mutation", [
    "failed_decoder", "previous_generation", "other_process", "stopped_pipe",
    "dead_pipe", "stalled_pipe", "recorded_exit",
])
def test_dead_or_superseded_pipe_does_not_suppress_recovery(mutation):
    runtime = finishing_runtime()
    if mutation == "failed_decoder":
        runtime._process.poll = lambda: 1
    elif mutation == "previous_generation":
        runtime._icecast_pipe_generation = 2
    elif mutation == "other_process":
        runtime._icecast_pipe_process = object()
    elif mutation == "stopped_pipe":
        runtime._icecast_pipe_stop.set()
    elif mutation == "dead_pipe":
        runtime._icecast_pipe_thread.is_alive = lambda: False
    elif mutation == "stalled_pipe":
        runtime._last_program_pcm_monotonic = time.monotonic() - 30
    elif mutation == "recorded_exit":
        runtime._producer_exit_generation = 3
        runtime._producer_exit_code = 0
    assert runtime.status()["producer_finalizing"] is False


def test_finishing_pipe_under_output_backpressure_retains_owned_audio():
    runtime = finishing_runtime()
    runtime._last_program_pcm_monotonic = time.monotonic() - 30
    runtime._program_fanout_inflight = 1
    runtime._program_fanout_started_monotonic = time.monotonic() - 30
    assert runtime.status()["producer_finalizing"] is True


@pytest.mark.parametrize("matching", [True, False])
def test_worker_drain_guard_includes_only_the_owned_finalizing_pipe(matching):
    worker = StationWorker.__new__(StationWorker)
    status = {
        "program_running": False, "producer_finalizing": True,
        "producer_draining": False, "producer_eof": False,
        "active_input_uri": "E:/Ads/PowerAPP.mp3",
    }
    uri = "E:/Ads/PowerAPP.mp3" if matching else "E:/Ads/Other.mp3"
    assert worker._runtime_source_is_draining(status, uri) is matching


def test_finalizing_ad_neither_restarts_nor_completes():
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker._ads_enabled = lambda: True
    worker._track_runtime_fields = lambda *_: ("E:/Ads/PowerAPP.mp3", "PowerAPP", "", "", "ad")
    worker.runtime_registry = SimpleNamespace(status=lambda *_: {
        "program_running": False, "producer_finalizing": True,
        "producer_eof": False, "active_input_uri": "E:/Ads/PowerAPP.mp3",
    })
    worker._restart_attempt_allowed = lambda *_: pytest.fail("finishing ad restarted")
    assert worker._restart_playing_ad_item_if_runtime_mismatched({"id": 518, "track_id": 52944}) is False


def test_exited_decoder_with_buffered_pipe_keeps_ad_until_full_fifo_eof():
    runtime = finishing_runtime()
    blocked, release = threading.Event(), threading.Event()

    class BufferedPipe:
        def __init__(self):
            self.reads = 0

        def read(self, _size):
            self.reads += 1
            if self.reads == 1:
                return bytes(4096)
            blocked.set()
            assert release.wait(2)
            return b""

        def close(self):
            release.set()

    class Sink:
        def __init__(self):
            self.accepted, self.drained = 0, 0

        def write_pcm(self, chunk):
            self.accepted += len(chunk)
            return True

        def is_running(self):
            return True

        def health_snapshot(self):
            return {"process_running": True, "queued_pcm_bytes": self.accepted - self.drained}

        def pcm_drain_watermark(self):
            return {"epoch": 1, "accepted_bytes": self.accepted, "drained_bytes": self.drained}

    runtime._icecast_pipe_thread = None
    runtime._icecast_pipe_process = None
    runtime._process.stdout = BufferedPipe()
    sink = Sink()
    runtime._icecast_sink = sink
    runtime._start_icecast_pipe_worker(runtime._process, sink, 3)
    try:
        assert blocked.wait(2)
        assert sink.accepted == 4096
        status = runtime.status()
        assert status["program_running"] is False
        assert status["producer_finalizing"] is True
        worker = StationWorker.__new__(StationWorker)
        assert worker._runtime_source_is_draining(status, "E:/Ads/PowerAPP.mp3") is True
        release.set()
        runtime._icecast_pipe_thread.join(2)
        assert runtime._icecast_pipe_thread.is_alive() is False
        status = runtime.status()
        assert status["producer_finalizing"] is False
        assert status["producer_draining"] is True
        assert status["producer_eof"] is False
        assert runtime.completed_producer_generation() is None
        sink.drained = sink.accepted
        assert runtime.status()["producer_eof"] is True
        assert runtime.completed_producer_generation() == 3
    finally:
        release.set()
        runtime._stop_icecast_pipe_worker()
