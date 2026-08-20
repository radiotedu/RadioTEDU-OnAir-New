from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


EXPECTED_STATION_IDS = (1, 2, 4, 5, 8, 9)


def verify(api_base: str, token_file: Path) -> dict[str, object]:
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("watchdog token is unavailable")
    request = Request(
        f"{api_base.rstrip('/')}/api/watchdog/status",
        headers={
            "Accept": "application/json",
            "X-RadioTEDU-Watchdog-Token": token,
            "User-Agent": "RadioTEDU-LiveRuntimeVerifier/1",
        },
    )
    with urlopen(request, timeout=15) as response:
        raw = response.read(128 * 1024 + 1)
        status = int(response.status)
    if len(raw) > 128 * 1024:
        raise RuntimeError("watchdog status response exceeded limit")
    payload = json.loads(raw.decode("utf-8"))
    stations = []
    issues: list[str] = []
    seen: set[int] = set()
    for item in payload.get("stations") or []:
        station_id = int(item.get("station_id") or 0)
        if station_id not in EXPECTED_STATION_IDS:
            continue
        seen.add(station_id)
        runtime = dict(item.get("runtime") or {})
        row = {
            "station_id": station_id,
            "genre": str(item.get("genre") or ""),
            "running": bool(runtime.get("running")),
            "worker_running": bool(runtime.get("worker_running")),
            "program_running": bool(runtime.get("program_running")),
            "input_present": bool(runtime.get("input_present")),
            "output_running": bool(runtime.get("output_running")),
            "mount_healthy": runtime.get("mount_healthy"),
            "encoder_error_count": int(runtime.get("encoder_error_count") or 0),
        }
        if not row["running"] or not row["worker_running"]:
            issues.append(f"station worker is not running: {station_id}")
        if not row["program_running"] or not row["input_present"]:
            issues.append(f"station programme is not running: {station_id}")
        if not row["output_running"]:
            issues.append(f"station source pipeline is not running: {station_id}")
        stations.append(row)
    missing = sorted(set(EXPECTED_STATION_IDS) - seen)
    if missing:
        issues.append("watchdog status omitted expected stations")
    stations.sort(key=lambda item: int(item["station_id"]))
    return {
        "ok": status == 200 and not issues,
        "http_status": status,
        "station_count": len(stations),
        "stations": stations,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only verification of live RadioTEDU station workers"
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:18110")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(r"C:\ProgramData\RadioTEDU\OnAir\secrets\watchdog-api.key"),
    )
    args = parser.parse_args()
    result = verify(args.api_base, args.token_file.expanduser().resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
