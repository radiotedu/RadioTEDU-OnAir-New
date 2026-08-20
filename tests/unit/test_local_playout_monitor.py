from __future__ import annotations

import json
import time

from tools.monitor_local_playout import ACTIVE_STATIONS, monitor


def _heartbeats(root, *, running=True, pcm_age=0.01):
    for station_id in ACTIVE_STATIONS:
        (root / f"station-{station_id}.heartbeat.json").write_text(
            json.dumps(
                {
                    "station_id": station_id,
                    "running": running,
                    "updated_epoch": time.time(),
                    "runtime_status": {
                        "program_running": running,
                        "program_pcm_age_seconds": pcm_age,
                        "program_pcm_stalled": False,
                    },
                }
            ),
            encoding="utf-8",
        )


def test_monitor_accepts_six_fresh_timelines(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _heartbeats(state)
    output = tmp_path / "result.json"
    assert monitor(state, 0.05, 0.01, output) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["failure_count"] == 0


def test_monitor_fails_closed_for_stopped_program(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _heartbeats(state)
    broken = state / "station-4.heartbeat.json"
    payload = json.loads(broken.read_text(encoding="utf-8"))
    payload["running"] = False
    payload["runtime_status"]["program_running"] = False
    payload["runtime_status"]["program_pcm_age_seconds"] = 3.0
    broken.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "result.json"
    assert monitor(state, 0.03, 0.01, output) == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert any(item["station_id"] == 4 for item in result["failures"])


def test_monitor_accepts_short_decoder_boundary_with_fresh_pcm(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _heartbeats(state)
    boundary = state / "station-4.heartbeat.json"
    payload = json.loads(boundary.read_text(encoding="utf-8"))
    payload["runtime_status"]["program_running"] = False
    payload["runtime_status"]["program_pcm_age_seconds"] = 0.8
    boundary.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "result.json"
    assert monitor(state, 0.03, 0.01, output) == 0
