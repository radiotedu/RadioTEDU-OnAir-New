from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


STATE_ROOT = Path(r"C:\ProgramData\RadioTEDU\OnAir\State\StationWorkers")
BACKUP_ROOT = Path(r"H:\RadioTEDU-Backups")
EXPECTED_STATIONS = (1, 2, 4, 5, 8, 9)
ADDITIONAL_OUTPUT_STATIONS = (10, 11)
ALLOWED_STATION_IDS = EXPECTED_STATIONS + ADDITIONAL_OUTPUT_STATIONS
WATCHDOG_TOKEN_PATH = Path(r"C:\ProgramData\RadioTEDU\OnAir\secrets\watchdog-api.key")


def _request_watchdog_restart(station_id: int) -> None:
    token = WATCHDOG_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("watchdog token is unavailable")
    request = Request(
        "http://127.0.0.1:18110/api/watchdog/repair",
        data=json.dumps({"station_ids": [station_id], "force_station_ids": [station_id], "repair_managed_profiles": False}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-RadioTEDU-Watchdog-Token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=360) as response:
            response.read(128 * 1024)
    except HTTPError as exc:
        if exc.code != 503:
            raise RuntimeError(f"watchdog restart rejected with HTTP {exc.code}") from None
        # A 503 can follow a completed restart whose output is still acquiring.
        # Verify the resulting worker instead of sending a second restart.


def _heartbeat_path(station_id: int) -> Path:
    return STATE_ROOT / f"station-{int(station_id)}.heartbeat.json"


def _read_heartbeat(station_id: int) -> dict:
    path = _heartbeat_path(station_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime(heartbeat: dict) -> dict:
    return dict(heartbeat.get("runtime_status") or {})


def _healthy(heartbeat: dict) -> bool:
    runtime = _runtime(heartbeat)
    health = dict(runtime.get("icecast_mount_health") or {})
    branches = dict(runtime.get("branch_health") or {})
    pcm_age = runtime.get("program_pcm_age_seconds")
    extra_health = [dict(item.get("health") or {}) for item in runtime.get("extra_icecast_mounts") or []]
    return bool(
        0 <= time.time() - float(heartbeat.get("updated_epoch") or 0) <= 5
        and heartbeat.get("running")
        and runtime.get("running")
        and runtime.get("program_running")
        and pcm_age is not None
        and float(pcm_age) <= 5.0
        and runtime.get("output_feed_active")
        and not runtime.get("program_pcm_stalled")
        and health.get("mount_healthy")
        and health.get("process_running")
        and health.get("writer_running")
        and not health.get("writer_failed")
        and not health.get("network_failed")
        and health.get("last_write_age_seconds") is not None
        and float(health["last_write_age_seconds"]) <= 5.0
        and health.get("last_network_write_age_seconds") is not None
        and float(health["last_network_write_age_seconds"]) <= 5.0
        and branches.get("icecast")
        and all(
            item.get("mount_healthy") and item.get("process_running")
            and item.get("writer_running") and not item.get("writer_failed")
            and not item.get("network_failed")
            and item.get("last_network_write_age_seconds") is not None
            and float(item["last_network_write_age_seconds"]) <= 5
            for item in extra_health
        )
    )


def _counter_snapshot(heartbeat: dict) -> dict[str, dict[str, int]]:
    runtime = _runtime(heartbeat)
    branches = {"primary": dict(runtime.get("icecast_mount_health") or {})}
    for item in runtime.get("extra_icecast_mounts") or []:
        branches[str(item.get("branch") or item.get("mount") or "extra")] = dict(item.get("health") or {})
    return {branch: {
        "encoded_bytes_sent": int(health.get("encoded_bytes_sent") or 0),
        "continuity_silence_chunks": int(health.get("continuity_silence_chunks") or 0),
        "dropped_pcm_chunks": int(health.get("dropped_pcm_chunks") or 0),
        "encoder_error_count": int(health.get("encoder_error_count") or 0),
        "network_error_count": int(health.get("network_error_count") or 0),
    } for branch, health in branches.items()}


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
        heartbeat = _read_heartbeat(station_id)
        generation = int(heartbeat.get("generation") or 0)
        for source in (
            _heartbeat_path(station_id),
            STATE_ROOT / f"station-{station_id}-g{generation}.json",
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
    restart_method: str = "watchdog",
) -> dict:
    before = _wait_until(
        station_id,
        lambda value: (restart_method == "watchdog" or _healthy(value))
        and not bool(_runtime(value).get("transition_active")),
        timeout_seconds=startup_timeout_seconds,
        description="a healthy non-transition playout state",
    )
    old_pid = int(before.get("pid") or 0)
    old_generation = int(before.get("generation") or 0)
    if old_pid <= 0:
        raise RuntimeError(f"station {station_id}: heartbeat has no worker PID")

    requested_epoch = time.time()
    if restart_method == "watchdog":
        _request_watchdog_restart(station_id)
    else:
        stop_path = STATE_ROOT / f"station-{station_id}.stop"
        stop_path.touch(exist_ok=True)

    replacement = _wait_until(
        station_id,
        lambda value: int(value.get("pid") or 0) not in {0, old_pid}
        and float(value.get("updated_epoch") or 0) >= requested_epoch
        and (restart_method == "watchdog" or int(value.get("generation") or 0) > old_generation)
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
    if set(finish_counters) != set(start_counters):
        raise RuntimeError(f"station {station_id}: output roster changed during verification")
    deltas = {branch: {key: finish_counters[branch][key] - values[key] for key in values} for branch, values in start_counters.items()}
    verified = bool(
        _healthy(finish)
        and all(values["encoded_bytes_sent"] > 0 and all(value == 0 for key, value in values.items() if key != "encoded_bytes_sent") for values in deltas.values())
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
    # Some stations need several minutes to reacquire a scheduled programme
    # after their isolated worker exits. Keep the other streams untouched
    # while allowing the selected station the full startup window.
    parser.add_argument("--startup-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--settle-seconds", type=float, default=12.0)
    parser.add_argument("--verify-seconds", type=float, default=20.0)
    parser.add_argument("--restart-method", choices=("watchdog", "stop-file"), default="watchdog")
    args = parser.parse_args()
    station_ids = tuple(args.station_id or EXPECTED_STATIONS)
    if not station_ids or any(value not in ALLOWED_STATION_IDS for value in station_ids):
        raise ValueError(f"station ids must be selected from {ALLOWED_STATION_IDS}")
    if len(set(station_ids)) != len(station_ids):
        raise ValueError("station ids must not repeat")

    live_files = {
        int(path.name.split(".", 1)[0].split("-")[1])
        for path in STATE_ROOT.glob("station-*.heartbeat.json")
    }
    missing_live_files = set(EXPECTED_STATIONS) - live_files
    if missing_live_files:
        raise RuntimeError(
            f"refusing rolling reload: protected stations are missing {sorted(missing_live_files)}; found {sorted(live_files)}"
        )
    for station_id in station_ids:
        if args.restart_method == "stop-file" and not _healthy(_read_heartbeat(station_id)):
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
            restart_method=args.restart_method,
        )
        results.append(result)
        print(json.dumps({"event": "station_verified", **result}, separators=(",", ":")), flush=True)
    print(json.dumps({"event": "complete", "results": results}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
