from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime
from pathlib import Path


STATE_ROOT = Path(r"C:\ProgramData\RadioTEDU\OnAir\State\StationWorkers")
BACKUP_ROOT = Path(r"H:\RadioTEDU-Backups")
EXPECTED_STATIONS = (1, 2, 4, 5, 8, 9)


def _heartbeat_path(station_id: int) -> Path:
    return STATE_ROOT / f"station-{int(station_id)}.heartbeat.json"


def _read_heartbeat(station_id: int) -> dict:
    path = _heartbeat_path(station_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime(heartbeat: dict) -> dict:
    return dict(heartbeat.get("runtime_status") or {})


def _flac_branch_healthy(runtime: dict) -> bool:
    branches = dict(runtime.get("branch_health") or {})
    return any(
        bool(value) and str(branch).casefold().endswith("-flac")
        for branch, value in branches.items()
    )


def _healthy(heartbeat: dict) -> bool:
    runtime = _runtime(heartbeat)
    health = dict(runtime.get("icecast_mount_health") or {})
    branches = dict(runtime.get("branch_health") or {})
    return bool(
        heartbeat.get("running")
        and runtime.get("running")
        and runtime.get("output_feed_active")
        and not runtime.get("program_pcm_stalled")
        and health.get("mount_healthy")
        and branches.get("icecast")
        and _flac_branch_healthy(runtime)
    )


def _counter_snapshot(heartbeat: dict) -> dict[str, int]:
    health = dict(_runtime(heartbeat).get("icecast_mount_health") or {})
    return {
        "encoded_bytes_sent": int(health.get("encoded_bytes_sent") or 0),
        "continuity_silence_chunks": int(health.get("continuity_silence_chunks") or 0),
        "dropped_pcm_chunks": int(health.get("dropped_pcm_chunks") or 0),
        "encoder_error_count": int(health.get("encoder_error_count") or 0),
        "network_error_count": int(health.get("network_error_count") or 0),
    }


def _wait_until(
    station_id: int,
    predicate,
    *,
    timeout_seconds: float,
    description: str,
) -> dict:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            last = _read_heartbeat(station_id)
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.25)
            continue
        if predicate(last):
            return last
        time.sleep(0.25)
    raise TimeoutError(f"station {station_id}: timed out waiting for {description}")


def _backup_state(station_ids: tuple[int, ...]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUP_ROOT / f"{stamp}-rolling-worker-reload"
    destination.mkdir(parents=True, exist_ok=False)
    for station_id in station_ids:
        for source in (
            _heartbeat_path(station_id),
            STATE_ROOT / f"station-{station_id}-g1.json",
        ):
            if source.is_file():
                shutil.copy2(source, destination / source.name)
    return destination


def _reload_one(
    station_id: int,
    *,
    startup_timeout_seconds: float,
    settle_seconds: float,
    verify_seconds: float,
) -> dict:
    before = _wait_until(
        station_id,
        lambda value: _healthy(value)
        and not bool(_runtime(value).get("transition_active")),
        timeout_seconds=startup_timeout_seconds,
        description="a healthy non-transition playout state",
    )
    old_pid = int(before.get("pid") or 0)
    old_generation = int(before.get("generation") or 0)
    if old_pid <= 0:
        raise RuntimeError(f"station {station_id}: heartbeat has no worker PID")

    # This is the manager's normal graceful-stop signal. desired_running stays
    # true in the parent, so it starts exactly one replacement generation.
    stop_path = STATE_ROOT / f"station-{station_id}.stop"
    stop_path.touch(exist_ok=True)

    replacement = _wait_until(
        station_id,
        lambda value: int(value.get("pid") or 0) not in {0, old_pid}
        and int(value.get("generation") or 0) > old_generation
        and _healthy(value),
        timeout_seconds=startup_timeout_seconds,
        description="a healthy replacement worker",
    )
    new_pid = int(replacement["pid"])
    new_generation = int(replacement["generation"])

    time.sleep(max(0.0, float(settle_seconds)))
    start = _read_heartbeat(station_id)
    if not _healthy(start):
        raise RuntimeError(f"station {station_id}: replacement became unhealthy during settle")
    start_counters = _counter_snapshot(start)
    time.sleep(max(1.0, float(verify_seconds)))
    finish = _read_heartbeat(station_id)
    finish_counters = _counter_snapshot(finish)
    deltas = {
        key: finish_counters[key] - start_counters[key]
        for key in start_counters
    }
    verified = bool(
        _healthy(finish)
        and deltas["encoded_bytes_sent"] > 0
        and deltas["continuity_silence_chunks"] == 0
        and deltas["dropped_pcm_chunks"] == 0
        and deltas["encoder_error_count"] == 0
        and deltas["network_error_count"] == 0
    )
    if not verified:
        raise RuntimeError(
            f"station {station_id}: replacement continuity verification failed: {deltas}"
        )
    return {
        "station_id": station_id,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "old_generation": old_generation,
        "new_generation": new_generation,
        "deltas": deltas,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gracefully reload isolated RadioTEDU workers one at a time."
    )
    parser.add_argument("--station-id", action="append", type=int, default=[])
    parser.add_argument("--startup-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--settle-seconds", type=float, default=12.0)
    parser.add_argument("--verify-seconds", type=float, default=20.0)
    args = parser.parse_args()
    station_ids = tuple(args.station_id or EXPECTED_STATIONS)
    if not station_ids or any(value not in EXPECTED_STATIONS for value in station_ids):
        raise ValueError(f"station ids must be selected from {EXPECTED_STATIONS}")
    if len(set(station_ids)) != len(station_ids):
        raise ValueError("station ids must not repeat")

    live_files = {
        int(path.name.split(".", 1)[0].split("-")[1])
        for path in STATE_ROOT.glob("station-*.heartbeat.json")
    }
    if live_files != set(EXPECTED_STATIONS):
        raise RuntimeError(
            f"refusing rolling reload: expected live stations {EXPECTED_STATIONS}, found {sorted(live_files)}"
        )
    for station_id in station_ids:
        if not _healthy(_read_heartbeat(station_id)):
            raise RuntimeError(f"station {station_id}: preflight health check failed")

    backup = _backup_state(station_ids)
    print(json.dumps({"event": "backup", "path": str(backup)}, separators=(",", ":")), flush=True)
    results = []
    for station_id in station_ids:
        result = _reload_one(
            station_id,
            startup_timeout_seconds=args.startup_timeout_seconds,
            settle_seconds=args.settle_seconds,
            verify_seconds=args.verify_seconds,
        )
        results.append(result)
        print(json.dumps({"event": "station_verified", **result}, separators=(",", ":")), flush=True)
    print(json.dumps({"event": "complete", "results": results}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
