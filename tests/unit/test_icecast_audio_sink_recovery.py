import queue
import threading

import app.audio.icecast_audio_sink as sink_module
from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_audio_sink import IcecastAudioSink


def test_output_recovery_preserves_queued_and_inflight_pcm():
    sink = IcecastAudioSink("ffmpeg", lambda *_args, **_kwargs: None)
    sink._pcm_queue.put_nowait(b"queued-programme-audio")
    sink._pcm_dispatch_queue.put_nowait(b"queued-dispatch-audio")
    sink._writer_pending_chunk = b"inflight-writer-audio"
    sink._pcm_dispatch_pending_chunk = b"inflight-dispatch-audio"

    sink.stop(preserve_pcm=True)

    assert sink._pcm_queue.get_nowait() == b"queued-programme-audio"
    assert sink._pcm_dispatch_queue.get_nowait() == b"queued-dispatch-audio"
    assert sink._writer_pending_chunk == b"inflight-writer-audio"
    assert sink._pcm_dispatch_pending_chunk == b"inflight-dispatch-audio"

    # Ordinary shutdown remains responsible for clearing media buffers.
    sink.stop()
    assert sink._pcm_queue.empty()
    assert sink._pcm_dispatch_queue.empty()
    assert sink._writer_pending_chunk is None
    assert sink._pcm_dispatch_pending_chunk is None


def test_preserved_stop_keeps_dispatch_fifo_open_for_programme_pcm():
    sink = IcecastAudioSink(
        "ffmpeg", lambda *_args, **_kwargs: None, decouple_input_backpressure=True
    )
    sink._pcm_dispatch_stop.clear()

    sink.stop(preserve_pcm=True)

    assert not sink._pcm_dispatch_stop.is_set()
    assert sink.write_pcm(b"during-output-reconnect") is True
    assert sink._pcm_dispatch_queue.get_nowait() == b"during-output-reconnect"


def test_dispatch_input_retries_full_fifo_until_space_is_available():
    sink = IcecastAudioSink(
        "ffmpeg", lambda *_args, **_kwargs: None, decouple_input_backpressure=True
    )
    sink._pcm_dispatch_queue = queue.Queue(maxsize=1)
    sink._pcm_dispatch_stop.clear()
    sink._pcm_dispatch_queue.put_nowait(b"first")
    result = []
    producer = threading.Thread(
        target=lambda: result.append(sink.write_pcm(b"second"))
    )
    producer.start()

    deadline = threading.Event()
    for _ in range(100):
        if sink._pcm_dispatch_backpressured:
            break
        deadline.wait(0.01)
    assert sink._pcm_dispatch_backpressured

    assert sink._pcm_dispatch_queue.get_nowait() == b"first"
    producer.join(timeout=1.0)
    assert not producer.is_alive()
    assert result == [True]
    assert sink._pcm_dispatch_queue.get_nowait() == b"second"
    sink.stop()


def test_dispatch_worker_resumes_same_pending_pcm_after_preserved_restart():
    sink = IcecastAudioSink(
        "ffmpeg", lambda *_args, **_kwargs: None, decouple_input_backpressure=True
    )
    sink._writer_stop.set()
    sink._pcm_dispatch_stop.clear()
    sink._pcm_dispatch_queue.put_nowait(b"queued-before-reconnect")
    dispatched = []
    delivered = threading.Event()

    def accept_pcm(payload, **_kwargs):
        dispatched.append(payload)
        delivered.set()
        return True

    sink._write_pcm_blocking = accept_pcm
    sink._start_pcm_dispatch_worker()
    assert sink._pcm_dispatch_queue.get_nowait() == b"queued-before-reconnect"

    # Requeue as an inflight frame to verify that it is retained across the
    # writer stop and delivered once the replacement writer is ready.
    sink._pcm_dispatch_pending_chunk = b"inflight-before-reconnect"
    sink.stop(preserve_pcm=True)
    sink._start_pcm_dispatch_worker()
    sink._writer_stop.clear()
    sink._writer_ready.set()

    assert delivered.wait(timeout=1.0)
    assert dispatched == [b"inflight-before-reconnect"]
    sink.stop()


def test_pcm_drain_watermark_distinguishes_dispatch_acceptance_from_fifo_drain():
    sink = IcecastAudioSink(
        "ffmpeg", lambda *_args, **_kwargs: None, decouple_input_backpressure=True
    )
    sink._pcm_dispatch_stop.clear()

    assert sink.write_pcm(b"programme-frame") is True
    admitted = sink.pcm_drain_watermark()
    assert admitted["accepted_bytes"] == len(b"programme-frame")
    assert admitted["drained_bytes"] == 0

    sink.stop(preserve_pcm=True)
    assert sink.pcm_drain_watermark() == admitted
    sink.stop()
    cleared = sink.pcm_drain_watermark()
    assert cleared["epoch"] > admitted["epoch"]
    assert cleared["accepted_bytes"] == cleared["drained_bytes"] == 0


def test_preserved_reconnect_bypasses_initial_spread_but_cold_start_keeps_it(
    monkeypatch,
):
    spreads = []

    def spread(_cfg, maximum):
        spreads.append(maximum)
        return 0.25

    monkeypatch.setattr(sink_module, "_mount_spread_seconds", spread)
    cfg = StationPipelineConfig(
        input_uri="C:/music/demo.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="test",
        icecast_enabled=True,
        local_output_enabled=False,
        output_device_id="",
    )

    def exercise_connector(*, preserve_pcm):
        source_started = threading.Event()

        def failing_source(_cfg):
            source_started.set()
            raise RuntimeError("simulated transient connection failure")

        sink = IcecastAudioSink(
            "ffmpeg",
            lambda *_args, **_kwargs: None,
            source_factory=failing_source,
            initial_connect_spread_sec=30.0,
        )
        class StoppedProcess:
            def poll(self):
                return 0

        sink._start_writer_worker = lambda: sink._writer_stop.clear()
        sink._start_pcm_dispatch_worker = lambda: None
        sink._start_probe_worker = lambda *_args, **_kwargs: None
        start_connector = sink._start_connector_worker

        def start_connector_and_return(cfg, *, preserve_pcm=False):
            start_connector(cfg, preserve_pcm=preserve_pcm)
            sink._process = StoppedProcess()

        sink._start_connector_worker = start_connector_and_return
        sink.ensure_started(cfg, preserve_pcm=preserve_pcm)
        return sink, source_started

    reconnect, reconnect_started = exercise_connector(preserve_pcm=True)
    assert reconnect_started.wait(timeout=0.1)
    assert 30.0 not in spreads
    reconnect.stop()
    spreads.clear()

    cold_start, cold_start_started = exercise_connector(preserve_pcm=False)
    assert not cold_start_started.wait(timeout=0.05)
    assert 30.0 in spreads
    assert cold_start_started.wait(timeout=0.5)
    cold_start.stop()
