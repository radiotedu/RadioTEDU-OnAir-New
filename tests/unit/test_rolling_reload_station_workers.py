import json
import time

import pytest

from tools import rolling_reload_station_workers as reload


def heartbeat(pid, generation, encoded, low_silence=0):
    health = {"mount_healthy": True, "process_running": True, "writer_running": True,
              "writer_failed": False, "network_failed": False, "last_write_age_seconds": 0.1,
              "last_network_write_age_seconds": 0.1, "encoded_bytes_sent": encoded}
    return {"updated_epoch": time.time(), "pid": pid, "generation": generation, "running": True,
            "runtime_status": {"running": True, "program_running": True, "output_feed_active": True,
                               "program_pcm_age_seconds": 0.1, "program_pcm_stalled": False,
                               "branch_health": {"icecast": True}, "icecast_mount_health": health,
                               "extra_icecast_mounts": [{"branch": "icecast:/classic-low",
                                                         "health": {**health, "continuity_silence_chunks": low_silence}}]}}


def test_healthy_requires_fresh_heartbeat_and_every_quality_output():
    value = heartbeat(123, 1, 100)
    assert reload._healthy(value)
    value["runtime_status"]["extra_icecast_mounts"][0]["health"]["writer_failed"] = True
    assert not reload._healthy(value)
    value = heartbeat(123, 1, 100)
    value["updated_epoch"] -= 20
    assert not reload._healthy(value)


def test_watchdog_restart_preserves_profiles_and_restarts_one_station(tmp_path, monkeypatch):
    token = tmp_path / "watchdog-test-token"
    token.write_text("local-test-token-with-sufficient-length")
    monkeypatch.setattr(reload, "WATCHDOG_TOKEN_PATH", token)
    requests = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b"{}"

    def request(value, **kwargs):
        requests.append(value)
        return Response()

    monkeypatch.setattr(reload, "urlopen", request)
    reload._request_watchdog_restart(1)
    assert len(requests) == 1
    assert json.loads(requests[0].data) == {"station_ids": [1], "force_station_ids": [1], "repair_managed_profiles": False}


def test_reload_accepts_new_manager_generation_and_rejects_quality_silence(monkeypatch):
    monkeypatch.setattr(reload.time, "time", lambda: 100.0)
    before = heartbeat(123, 5, 100)
    replacement = heartbeat(456, 1, 200)
    wait_rows = iter([before, replacement])

    def wait(station, predicate, **kwargs):
        row = next(wait_rows)
        assert predicate(row)
        return row

    samples = iter([heartbeat(456, 1, 200), heartbeat(456, 1, 500, low_silence=1)])
    monkeypatch.setattr(reload, "_wait_until", wait)
    monkeypatch.setattr(reload, "_read_heartbeat", lambda station: next(samples))
    monkeypatch.setattr(reload, "_request_watchdog_restart", lambda station: None)
    monkeypatch.setattr(reload.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="continuity verification failed"):
        reload._reload_one(1, startup_timeout_seconds=10, settle_seconds=0, verify_seconds=1)
