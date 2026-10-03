import copy
import time

import pytest

from tools.rolling_reload_station_workers import _healthy


def healthy_heartbeat():
    health = {
        "mount_healthy": True,
        "source_response_confirmed": True,
        "process_running": True,
        "writer_running": True,
        "writer_failed": False,
        "network_failed": False,
        "last_write_age_seconds": 0,
        "last_network_write_age_seconds": 0,
    }
    return {
        "updated_epoch": time.time(), "running": True,
        "runtime_status": {
            "running": True, "program_running": True,
            "program_pcm_age_seconds": 0, "output_feed_active": True,
            "branch_health": {"icecast": True},
            "icecast_mount_health": health,
            "extra_icecast_mounts": [{"mount": "/test-low", "health": copy.deepcopy(health)}],
        },
    }


def test_all_sources_must_have_final_acceptance():
    assert _healthy(healthy_heartbeat()) is True


@pytest.mark.parametrize("branch", ["primary", "extra"])
@pytest.mark.parametrize("confirmation", [False, None, "true", 1])
def test_optimistic_socket_progress_cannot_certify_reload(branch, confirmation):
    heartbeat = healthy_heartbeat()
    runtime = heartbeat["runtime_status"]
    health = runtime["icecast_mount_health"] if branch == "primary" else runtime["extra_icecast_mounts"][0]["health"]
    health["source_response_confirmed"] = confirmation
    assert _healthy(heartbeat) is False
