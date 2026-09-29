import json

from app.engine.process_worker_child import (
    _runtime_status_for_liveness,
    _transport_health_for_liveness,
    _transport_is_healthy,
    _write_heartbeat,
)


def _healthy_mount(*, process_running=None):
    health = {
        "mount_healthy": True,
        "remote_mount_verified": True,
        "writer_running": True,
        "writer_failed": False,
        "last_write_age_seconds": 0.1,
        "network_writer_running": True,
        "network_failed": False,
        "last_network_write_age_seconds": 0.1,
        "writer_backpressured": False,
        "writer_backpressure_age_seconds": 0.0,
        "queued_pcm_seconds": 5.0,
        "pcm_queue_capacity_chunks": 1024,
    }
    if process_running is not None:
        health["process_running"] = process_running
    return health


def _healthy_required_outputs_status():
    return {
        "program_running": True,
        "program_pcm_stalled": False,
        "program_pcm_age_seconds": 0.1,
        "required_outputs": {
            "icecast": True,
            "local": False,
            "icecast:/situation-low": True,
            "icecast:/situation-flac": True,
        },
        "branch_health": {
            "icecast": True,
            "icecast:/situation-low": True,
            "icecast:/situation-flac": True,
        },
        "delivery_health": {
            "icecast": True,
            "icecast:/situation-low": True,
            "icecast:/situation-flac": True,
        },
        "icecast_sink_running": True,
        "icecast_mount_health": _healthy_mount(process_running=True),
        "extra_icecast_mounts": [
            {
                "branch": "icecast:/situation-low",
                "mount": "/situation-low",
                "health": _healthy_mount(),
            },
            {
                "branch": "icecast:/situation-flac",
                "mount": "/situation-flac",
                "health": _healthy_mount(),
            },
        ],
    }


def test_transport_is_unhealthy_when_required_primary_mount_fails():
    status = _healthy_required_outputs_status()
    status["delivery_health"]["icecast"] = False
    status["icecast_mount_health"].update(
        {
            "mount_healthy": False,
            "process_running": False,
            "network_failed": True,
            "last_network_write_age_seconds": 12.0,
        }
    )

    assert _transport_is_healthy(status) is False


def test_transport_defaults_primary_to_required_when_map_is_missing():
    status = _healthy_required_outputs_status()
    status["required_outputs"] = {}
    status["delivery_health"]["icecast"] = False
    status["icecast_mount_health"]["mount_healthy"] = False

    assert _transport_is_healthy(status) is False


def test_transport_checks_secondary_mounts_when_legacy_map_omits_them():
    status = _healthy_required_outputs_status()
    status["required_outputs"] = {}
    status["extra_icecast_mounts"][0]["health"]["network_failed"] = True

    assert _transport_is_healthy(status) is False


def test_transport_is_healthy_when_every_enabled_output_is_healthy():
    assert _transport_is_healthy(_healthy_required_outputs_status()) is True


def test_transport_is_unhealthy_when_required_secondary_mount_fails():
    status = _healthy_required_outputs_status()
    status["delivery_health"]["icecast:/situation-low"] = False
    status["extra_icecast_mounts"][0]["health"]["mount_healthy"] = False

    assert _transport_is_healthy(status) is False


def test_transport_is_unhealthy_when_secondary_mount_queue_stays_saturated():
    status = _healthy_required_outputs_status()
    status["extra_icecast_mounts"][0]["health"].update(
        {
            "writer_backpressured": True,
            "writer_backpressure_age_seconds": 6007.0,
            "queued_pcm_seconds": 21.8,
            "pcm_queue_capacity_chunks": 1024,
        }
    )

    assert _transport_is_healthy(status) is False


def test_transport_requires_successful_probe_for_every_required_icecast_mount():
    status = _healthy_required_outputs_status()
    status["icecast_mount_health"]["remote_mount_verified"] = False

    assert _transport_is_healthy(status) is False

    status = _healthy_required_outputs_status()
    status["extra_icecast_mounts"][0]["health"]["remote_mount_verified"] = False

    assert _transport_is_healthy(status) is False


def test_liveness_status_read_failure_fails_closed_and_persists_diagnostic(tmp_path):
    last_status = _healthy_required_outputs_status()

    class BrokenRuntimeRegistry:
        def status(self, _station_id):
            raise RuntimeError("status_lock_unavailable")

    status, available, error = _runtime_status_for_liveness(
        BrokenRuntimeRegistry(), 5, last_status
    )
    transport_healthy = _transport_health_for_liveness(status, available)
    assert status == last_status
    assert available is False
    assert error == "RuntimeError"
    assert transport_healthy is False

    heartbeat_path = tmp_path / "worker.heartbeat.json"
    _write_heartbeat(
        {"heartbeat_path": str(heartbeat_path), "station_id": 5},
        {
            "runtime_status_available": available,
            "runtime_status_error": error,
            "transport_healthy": transport_healthy,
        },
        runtime_status=status,
        running=True,
    )

    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["runtime_status_available"] is False
    assert heartbeat["runtime_status_error"] == "RuntimeError"
    assert heartbeat["transport_healthy"] is False
