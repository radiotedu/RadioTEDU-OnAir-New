from copy import deepcopy

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.station_runtime import StationRuntime
from app.engine.process_worker_child import _transport_is_healthy
from app.engine.runtime_supervisor import RuntimeSupervisor
from app.engine.runtime_registry import StationRuntimeRegistry
from app.api.health_wall import _local_programme_is_active


def _writer_health():
    return {
        "process_running": True, "mount_healthy": True,
        "remote_mount_verified": False,
        "writer_running": True, "network_writer_running": True,
        "writer_failed": False, "network_failed": False,
        "last_write_age_seconds": 0.1,
        "last_network_write_age_seconds": 0.1,
        "writer_backpressured": False,
    }


def _status():
    return {
        "running": True, "program_running": True,
        "program_pcm_age_seconds": 0.1, "program_pcm_stalled": False,
        "icecast_sink_running": True,
        "required_outputs": {"icecast": True, "local": False},
        "branch_health": {"icecast": True},
        "source_health": {"icecast": True},
        "delivery_health": {"icecast": False},
        "icecast_mount_health": _writer_health(),
    }


class _Registry:
    def __init__(self, status):
        self.value = status
        self.recovered = []

    def status(self, station_id):
        return self.value

    def recover_station_primary_output(self, station_id):
        self.recovered.append(station_id)


def test_listener_verification_failure_does_not_restart_a_healthy_source():
    registry = _Registry(_status())
    assert RuntimeSupervisor(registry).evaluate_station(4)["action"] == "none"
    assert registry.recovered == []
    assert registry.value["delivery_health"]["icecast"] is False


def test_a_failed_source_still_recovers_while_listener_evidence_is_missing():
    value = _status()
    value["source_health"]["icecast"] = False
    registry = _Registry(value)
    assert RuntimeSupervisor(registry).evaluate_station(4)["action"] == "recover_primary_output"
    assert registry.recovered == [4]


def test_worker_source_liveness_requires_fresh_writes_even_without_listener_probe():
    value = _status()
    assert _transport_is_healthy(value) is True
    failed = deepcopy(value)
    failed["icecast_mount_health"]["last_network_write_age_seconds"] = 8.0
    assert _transport_is_healthy(failed) is False
    failed = deepcopy(value)
    failed["source_health"]["icecast"] = False
    assert _transport_is_healthy(failed) is False
    failed = deepcopy(value)
    failed.pop("source_health")
    failed["delivery_health"]["icecast"] = True
    assert _transport_is_healthy(failed) is False


def test_worker_liveness_checks_each_enabled_quality_source():
    value = _status()
    branch = "icecast:/radio-low"
    value["required_outputs"][branch] = True
    value["branch_health"][branch] = True
    value["source_health"][branch] = True
    value["delivery_health"][branch] = False
    value["extra_icecast_mounts"] = [{"branch": branch, "health": _writer_health()}]
    assert _transport_is_healthy(value) is True
    value["extra_icecast_mounts"][0]["health"]["writer_running"] = False
    assert _transport_is_healthy(value) is False


def test_runtime_reports_source_health_without_claiming_verified_public_audio(monkeypatch):
    class _Sink:
        def is_running(self):
            return True

        def health_snapshot(self):
            return _writer_health()

    runtime = StationRuntime()
    runtime._icecast_sink = _Sink()
    runtime._active_cfg = StationPipelineConfig(
        input_uri="C:/music/track.flac", icecast_host="example.invalid",
        icecast_port=8000, icecast_mount="/radio", icecast_user="source",
        icecast_password="", local_output_enabled=False, output_device_id="",
        icecast_enabled=True,
    )
    monkeypatch.setattr(runtime, "branch_health", lambda: {"icecast": True, "local": False})
    monkeypatch.setattr(runtime, "_program_running", lambda: True)
    monkeypatch.setattr(runtime, "_output_feed_active", lambda: True)
    value = runtime.status()
    assert value["source_health"]["icecast"] is True
    assert value["delivery_health"]["icecast"] is False
    assert value["public_listener_health"]["icecast"] is False


def test_queue_output_readiness_uses_source_writes_separately_from_public_delivery():
    value = _status()

    class _Runtime:
        def branch_health(self):
            return value["branch_health"]

        def status(self):
            return value

        def is_running(self):
            return True

    registry = StationRuntimeRegistry.__new__(StationRuntimeRegistry)
    registry._runtimes = {4: _Runtime()}
    registry._required_outputs = {4: {"icecast": True, "local": False}}
    assert registry.required_outputs_healthy(4) is True
    value["icecast_mount_health"]["last_network_write_age_seconds"] = 8.0
    assert registry.required_outputs_healthy(4) is False


def test_operator_programme_metadata_requires_current_real_source_audio():
    value = {"program_running": True, "program_pcm_stalled": False,
             "active_input_uri": "C:/music/track.flac", "program_pcm_age_seconds": 0.1}
    assert _local_programme_is_active(value) is True
    assert _local_programme_is_active({**value, "program_pcm_age_seconds": 8.0}) is False
    assert _local_programme_is_active({**value, "program_pcm_age_seconds": float("nan")}) is False
    assert _local_programme_is_active({**value, "active_input_uri": "silence://continuity"}) is False
    assert _local_programme_is_active({**value, "program_running": False}) is False


def test_local_public_snapshot_can_skip_blocking_listener_requests(monkeypatch):
    import app.api.public as public
    from app.db import get_connection, init_db
    from app.repositories.station_output_repo import StationOutputRepository

    init_db()
    conn = get_connection()
    try:
        StationOutputRepository(conn).upsert(
            1, False, "", True, "example.invalid", 8000, "/radio", "source", "",
        )
    finally:
        conn.close()

    monkeypatch.setattr(public.runtime_registry, "status", lambda _sid: _status())
    monkeypatch.setattr(public.worker_loop_manager, "status", lambda _sid: {"running": True})

    def unexpected_listener_request(*_args, **_kwargs):
        raise AssertionError("local snapshot must not wait for a listener request")

    monkeypatch.setattr(public, "_probe_icecast_origin", unexpected_listener_request)
    result = public.list_public_station_summaries(probe_origin=False)
    assert result["stations"]
    assert result["stations"][0]["status"] != "live"
