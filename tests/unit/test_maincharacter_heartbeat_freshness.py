import json
import time

import pytest

from tools import radiotedu_mini_monitor as monitor
from tools import verify_live_runtime as verifier


_MISSING = object()


def _write_heartbeat(path, tick_age=_MISSING):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "station_id": 11,
        "running": True,
        "scheduler_stalled": False,
        "updated_epoch": time.time(),
        "runtime_status": {
            "running": True,
            "program_running": True,
            "output_feed_active": True,
            "icecast_sink_running": True,
            "icecast_mount_health": {"process_running": True, "mount_healthy": True},
        },
    }
    if tick_age is not _MISSING:
        payload["scheduler_tick_age_seconds"] = tick_age
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_verifier_accepts_zero_tick_age_and_rejects_missing_or_null(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    path = (
        tmp_path
        / "RadioTEDU"
        / "OnAir"
        / "State"
        / "StationWorkers"
        / "station-11.heartbeat.json"
    )

    _write_heartbeat(path, 0.0)
    assert verifier._maincharacter_station_row()["worker_running"] is True

    for missing_value in (_MISSING, None):
        _write_heartbeat(path, missing_value)
        row = verifier._maincharacter_station_row()
        assert row is not None
        assert row["worker_running"] is False


def test_mini_monitor_accepts_zero_tick_age_and_rejects_missing_or_null(tmp_path, monkeypatch):
    path = tmp_path / "station-11.heartbeat.json"
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)

    _write_heartbeat(path, 0.0)
    assert monitor._read_maincharacter_runtime() is not None

    for missing_value in (_MISSING, None):
        _write_heartbeat(path, missing_value)
        assert monitor._read_maincharacter_runtime() is None
