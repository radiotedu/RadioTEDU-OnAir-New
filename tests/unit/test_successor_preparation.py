import io
import time
from dataclasses import replace
from types import SimpleNamespace

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.station_runtime import StationRuntime
from app.engine.station_worker import StationWorker


class Process:
    def __init__(self, pcm):
        self.stdout = io.BytesIO(pcm)
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = self.code or 0
        return self.code

    def terminate(self):
        self.code = -1


def runtime(tmp_path):
    source = tmp_path / "next.wav"
    source.write_bytes(b"file identity")
    pcm = bytes(range(256)) * 4096
    launches = []

    def spawn(*args, **kwargs):
        process = Process(pcm)
        launches.append(process)
        return process

    rt = StationRuntime(process_factory=spawn)
    rt.ffmpeg_bin = "ffmpeg.exe"
    rt._active_cfg = StationPipelineConfig(
        input_uri="old.wav", icecast_host="localhost", icecast_port=8000,
        icecast_mount="/test", icecast_user="source", icecast_password="private",
        local_output_enabled=False, output_device_id="", loudness_target_lufs=-23,
    )
    rt._should_use_live_mix = lambda: False
    rt._producer_exit_drain_state = lambda **kw: (False, True)
    rt._icecast_output_targets = lambda: [("icecast", SimpleNamespace())]
    return rt, source, launches, pcm


def wait_ready(rt):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if rt._prepared_source.snapshot()["ready"]:
            return
        time.sleep(.002)
    raise AssertionError("not ready")


def test_preparation_has_no_ownership_or_sink_side_effects(tmp_path):
    rt, source, launches, pcm = runtime(tmp_path)
    previous = rt._active_cfg
    generation = rt._playout_generation
    rt.prepare_next_source(str(source), track_type="ad")
    wait_ready(rt)
    assert rt._active_cfg is previous
    assert rt._playout_generation == generation
    assert rt._process is None
    assert len(launches) == 1
    assert rt._decoder_preparation_status()["buffered_seconds"] == 4
    cfg = replace(previous, input_uri=str(source), track_type="ad", stream_title="PowerAPP")
    process = rt._spawn_icecast_pcm_producer(cfg)
    assert len(launches) == 1
    assert process.stdout.read() == pcm
    assert rt._decoder_preparation_status()["prepared_handoff_count"] == 1
    rt._terminate_owned_processes()


def test_repeated_preparation_is_bounded_to_one_decoder(tmp_path):
    rt, source, launches, _ = runtime(tmp_path)
    rt.prepare_next_source(str(source))
    wait_ready(rt)
    for _ in range(10):
        rt.prepare_next_source(str(source))
    assert len(launches) == 1
    rt.cancel_prepared_source()


def test_changed_audio_filters_reject_prepared_decoder(tmp_path, monkeypatch):
    import app.audio.ffmpeg_pipeline as pipeline
    rt, source, launches, _ = runtime(tmp_path)
    rt.prepare_next_source(str(source))
    wait_ready(rt)
    chain = pipeline._programme_filter_chain
    monkeypatch.setattr(pipeline, "_programme_filter_chain",
                        lambda cfg: [*chain(cfg), "volume=1.0"])
    rt._spawn_icecast_pcm_producer(replace(rt._active_cfg, input_uri=str(source)))
    assert len(launches) == 2
    assert launches[0].code == -1
    rt._terminate_owned_processes()


def test_changed_file_rejects_stale_prepared_audio(tmp_path):
    rt, source, launches, _ = runtime(tmp_path)
    rt.prepare_next_source(str(source))
    wait_ready(rt)
    source.write_bytes(b"a new version with different contents")
    rt._spawn_icecast_pcm_producer(replace(rt._active_cfg, input_uri=str(source)))
    assert len(launches) == 2 and launches[0].code == -1
    rt._terminate_owned_processes()


def test_seek_recovery_uses_new_decoder_at_correct_offset(tmp_path):
    rt, source, launches, _ = runtime(tmp_path)
    rt.prepare_next_source(str(source))
    wait_ready(rt)
    rt._spawn_icecast_pcm_producer(replace(rt._active_cfg, input_uri=str(source)),
                                  start_offset_seconds=20)
    assert len(launches) == 2 and launches[0].code == -1
    rt._terminate_owned_processes()


def test_no_preparation_before_clean_eof_or_during_live_show(tmp_path):
    rt, source, launches, _ = runtime(tmp_path)
    rt._producer_exit_drain_state = lambda **kw: (False, False)
    assert rt.prepare_next_source(str(source)) is False
    rt._producer_exit_drain_state = lambda **kw: (False, True)
    rt._should_use_live_mix = lambda: True
    assert rt.prepare_next_source(str(source)) is False
    assert launches == []


def test_scheduler_prepares_due_ad_without_marking_any_item_playing():
    calls = []
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker.runtime_registry = SimpleNamespace(
        status=lambda sid: {"producer_draining": True},
        prepare_next_station_source=lambda *a, **k: calls.append((a, k)),
    )
    worker._next_due_ad_if_allowed = lambda: {"id": 3, "track_id": 30}
    worker.schedule_repo = SimpleNamespace(next_ready=lambda sid: {"track_id": 40})
    worker.queue_repo = SimpleNamespace(next_pending=lambda sid: {"track_id": 50})
    worker.program_queue_repo = SimpleNamespace(get_source=lambda sid: "auto")
    worker._track_runtime_fields = lambda tid: (f"{tid}.wav", "PowerAPP", "", "", "ad")
    worker._prepare_successor_while_draining()
    assert calls == [((4, "30.wav"), {"stream_title": "PowerAPP", "stream_artist": "",
                                      "stream_album": "", "track_type": "ad"})]
    worker._prepare_successor_while_draining({"status": "live"})
    assert len(calls) == 1


def test_boundary_notification_keeps_full_fifo_completion_gate(tmp_path):
    rt, _, _, _ = runtime(tmp_path)
    rt._playout_generation = 7
    rt._producer_exit_generation = 7
    assert rt.completed_producer_generation() is None
    rt._producer_exit_drain_state = lambda **kw: (kw["current"], False)
    assert rt.completed_producer_generation() == 7
    rt._producer_exit_generation = 6
    assert rt.completed_producer_generation() is None


def test_child_wakes_once_for_completed_generation():
    from app.engine.process_worker_child import _new_completed_producer_generation
    registry = SimpleNamespace(completed_producer_generation=lambda sid: 8)
    assert _new_completed_producer_generation(registry, 4, 7) == 8
    assert _new_completed_producer_generation(registry, 4, 8) is None
    registry.completed_producer_generation = lambda sid: None
    assert _new_completed_producer_generation(registry, 4, 7) is None
    assert _new_completed_producer_generation(SimpleNamespace(), 4, 7) is None


def test_no_api_payload_is_built_in_worker_without_websocket_clients(monkeypatch):
    from app.api import legacy
    from app.ws.broadcaster import broadcaster
    monkeypatch.setattr(broadcaster.manager, "presence", lambda: {"count": 0})
    calls = []
    monkeypatch.setattr(legacy, "legacy_liquidsoap_status", lambda **k: calls.append(k))
    monkeypatch.setattr(legacy, "list_legacy_queue", lambda **k: calls.append(k))
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker._broadcast_worker_state(include_queue=True, include_track=True)
    assert calls == []


def test_clients_in_same_process_still_receive_worker_notifications(monkeypatch):
    from app.api import legacy
    from app.ws.broadcaster import broadcaster
    monkeypatch.setattr(broadcaster.manager, "presence", lambda: {"count": 1})
    monkeypatch.setattr(legacy, "legacy_liquidsoap_status", lambda **k: {"running": True})
    monkeypatch.setattr(legacy, "list_legacy_queue", lambda *a, **k: {"items": []})
    calls = []
    for name in ("on_runtime_updated", "on_engine_event", "on_track_changed", "on_queue_changed"):
        monkeypatch.setattr(broadcaster, name, lambda sid, payload, kind=name: calls.append(kind))
    worker = StationWorker.__new__(StationWorker)
    worker.station_id = 4
    worker._broadcast_worker_state(include_queue=True, include_track=True)
    assert calls == ["on_runtime_updated", "on_engine_event", "on_track_changed", "on_queue_changed"]
