from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


ACTIVE_STATIONS = {1, 2, 4, 5, 8, 9}


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _read_heartbeat(path: Path) -> dict:
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise RuntimeError("unreachable")


def monitor(state_root: Path, duration: float, interval: float, output: Path) -> int:
    started = time.monotonic()
    samples = 0
    failures: list[dict] = []
    worst_heartbeat_age = 0.0
    worst_pcm_age = 0.0
    while time.monotonic() - started < duration:
        for station_id in sorted(ACTIVE_STATIONS):
            heartbeat_path = state_root / f"station-{station_id}.heartbeat.json"
            try:
                heartbeat = _read_heartbeat(heartbeat_path)
                runtime = dict(heartbeat.get("runtime_status") or {})
                heartbeat_age = max(
                    0.0, time.time() - float(heartbeat.get("updated_epoch") or 0.0)
                )
                pcm_age = float(runtime.get("program_pcm_age_seconds") or 0.0)
                worst_heartbeat_age = max(worst_heartbeat_age, heartbeat_age)
                worst_pcm_age = max(worst_pcm_age, pcm_age)
                reasons = []
                if int(heartbeat.get("station_id") or 0) != station_id:
                    reasons.append("station_identity_mismatch")
                if not bool(heartbeat.get("running")):
                    reasons.append("worker_not_running")
                if heartbeat_age > 5.0:
                    reasons.append("stale_heartbeat")
                program_running = bool(runtime.get("program_running"))
                # A decoder can report exited during the sub-second boundary
                # between two tracks while the sink is still carrying the last
                # buffered PCM and the station worker is already starting the
                # next decoder.  That is not dead air.  Fail only when the
                # boundary exceeds the two-second PCM continuity budget.
                if not program_running and pcm_age > 2.0:
                    reasons.append("program_not_running")
                if bool(runtime.get("program_pcm_stalled")) or pcm_age > 2.0:
                    reasons.append("pcm_stalled")
                if reasons:
                    failures.append(
                        {
                            "station_id": station_id,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "reasons": reasons,
                            "heartbeat_age_seconds": round(heartbeat_age, 3),
                            "pcm_age_seconds": round(pcm_age, 3),
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "station_id": station_id,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "reasons": ["heartbeat_unreadable"],
                        "error": str(exc)[:200],
                    }
                )
        samples += 1
        remaining = duration - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(min(interval, remaining))
    payload = {
        "ok": not failures,
        "duration_seconds": round(time.monotonic() - started, 3),
        "samples": samples,
        "stations": sorted(ACTIVE_STATIONS),
        "failure_count": len(failures),
        "failures": failures[:100],
        "worst_heartbeat_age_seconds": round(worst_heartbeat_age, 3),
        "worst_pcm_age_seconds": round(worst_pcm_age, 3),
        "scope": "local authoritative timelines; public listener verification is separate",
    }
    _atomic_write(output, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor all six local RadioTEDU program timelines without exposing secrets."
    )
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=600.0)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("durations must be positive")
    return monitor(
        args.state_root.resolve(),
        args.duration_seconds,
        args.interval_seconds,
        args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
