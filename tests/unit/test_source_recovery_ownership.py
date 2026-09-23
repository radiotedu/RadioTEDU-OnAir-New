import pytest

from tests.unit.test_runtime_registry import _FlowingUnverifiedRuntime
from app.engine.runtime_registry import StationRuntimeRegistry


@pytest.mark.parametrize("starting", [True, False])
@pytest.mark.parametrize("producer_stalled", [True, False])
def test_registry_leaves_reconnecting_sources_and_siblings_running(starting, producer_stalled):
    runtime = _FlowingUnverifiedRuntime()
    snapshot = runtime.status()
    snapshot["program_pcm_stalled"] = producer_stalled
    snapshot["program_pcm_age_seconds"] = 60.0 if producer_stalled else 0.01
    health = snapshot["icecast_mount_health"]
    health.update(network_writer_running=True, writer_backpressured=True,
                  process_running=not starting,
                  last_write_age_seconds=None if starting else 0.01)
    snapshot["extra_icecast_mounts"] = [
        {"branch": "icecast:/lofi-low", "health": dict(health)}
    ]
    runtime.status = lambda: snapshot
    registry = StationRuntimeRegistry(runtime_factory=lambda: runtime)
    registry._runtimes[2] = runtime
    registry._required_outputs[2] = {"icecast": True, "icecast:/lofi-low": True}
    registry.recover_station(2)
    assert runtime.recover_calls == 0
    assert registry._recovery_state[2]["state"] == "monitoring"


@pytest.mark.parametrize("failure", ["writer_failed", "writer_stopped", "connector_stopped", "missing_extra"])
def test_registry_still_recovers_unmanaged_or_failed_output_workers(failure):
    runtime = _FlowingUnverifiedRuntime()
    snapshot = runtime.status()
    health = snapshot["icecast_mount_health"]
    health.update(network_writer_running=True)
    extra = dict(health)
    if failure == "writer_failed":
        extra["writer_failed"] = True
    elif failure == "writer_stopped":
        extra["writer_running"] = False
    elif failure == "connector_stopped":
        extra["network_writer_running"] = False
    snapshot["extra_icecast_mounts"] = [] if failure == "missing_extra" else [
        {"branch": "icecast:/lofi-low", "health": extra}
    ]
    runtime.status = lambda: snapshot
    registry = StationRuntimeRegistry(runtime_factory=lambda: runtime)
    registry._required_outputs[2] = {"icecast": True, "icecast:/lofi-low": True}
    assert not registry._unverified_icecast_transport_is_flowing(2, runtime)
