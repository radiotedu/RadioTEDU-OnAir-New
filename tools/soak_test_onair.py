"""Fail-closed RadioTEDU OnAir health and stream soak runner.

The runner is deliberately read-only.  It samples the authenticated local API,
optionally samples a public listener URL, and appends compact, secret-free
evidence to a JSONL file.  It is intended for a 24-hour (or longer) supervised
observation window; a short ``--once`` run is useful for commissioning checks.

Credentials are read from a protected file and are never accepted on the
command line or written to evidence.  A failed probe is recorded and the
process exits non-zero after the requested observation window, so operators
cannot accidentally treat an incomplete soak as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_SENSITIVE = re.compile(
    r"(?:pass(?:word)?|secret|token|api[_-]?key|credential|authorization|cookie|refresh)",
    re.IGNORECASE,
)
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
_DEFAULT_OUTPUT = Path(r"C:\ProgramData\RadioTEDU\OnAir\Recovery\soak")


class SoakError(RuntimeError):
    """A configuration or safety error which prevents a valid soak."""


@dataclass(frozen=True)
class Config:
    api_base: str
    username: str
    password_file: Path
    stream_url: str | None
    station_id: int | None
    interval_seconds: float
    duration_seconds: float
    output: Path
    timeout_seconds: float
    once: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_url(value: str, label: str, *, api: bool = False) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SoakError(f"{label} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query:
        raise SoakError(f"{label} must not contain credentials or query parameters")
    if api and parsed.hostname.lower() not in _LOOPBACK_NAMES and parsed.scheme != "https":
        raise SoakError("api_base must be loopback HTTP or HTTPS")
    return value.rstrip("/")


def _safe_file(value: str, label: str, *, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise SoakError(f"{label} must be an absolute, non-symlink path")
    if must_exist and not path.is_file():
        raise SoakError(f"{label} must reference an existing file")
    if path.exists() and not path.is_file():
        raise SoakError(f"{label} must reference a regular file")
    return path


def _read_password(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise SoakError("password file could not be read; use an elevated account") from exc
    # The provisioner writes a small labelled text file.  Do not echo or retain
    # the file contents in evidence; extract only the password line in memory.
    for line in value.splitlines():
        if line.lower().startswith("password:"):
            value = line.split(":", 1)[1].strip()
            break
    if not value or len(value) < 8:
        raise SoakError("password file does not contain a usable credential")
    return value


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, non-sensitive projection suitable for evidence."""
    if depth > 3:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE.search(name):
                continue
            if name.lower() in {"password_hash", "stream_url", "source_url", "mount", "config"}:
                continue
            result[name] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and _SENSITIVE.search(value):
            return "<redacted>"
        return value
    return str(type(value).__name__)


def _find_values(value: Any, names: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in names:
                found.append(item)
            found.extend(_find_values(item, names))
    elif isinstance(value, list):
        for item in value[:100]:
            found.extend(_find_values(item, names))
    return found


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _runtime_projection(payload: Any) -> dict[str, Any]:
    states = [str(x) for x in _find_values(payload, {"state", "lifecycle", "status"}) if x is not None]
    bytes_seen = [_number(x) for x in _find_values(payload, {"bytes_sent", "bytes_written", "encoded_bytes", "output_bytes"})]
    reconnects = [_number(x) for x in _find_values(payload, {"reconnects", "reconnect_count", "source_reconnects"})]
    encoder_errors = [_number(x) for x in _find_values(payload, {"encoder_error_count"})]
    encoder_error_lines = [
        x for x in _find_values(payload, {"last_encoder_error"}) if isinstance(x, str) and x.strip()
    ]
    tracks = [
        x
        for x in _find_values(
            payload,
            {"current_track", "now_playing", "title", "song", "active_input_uri", "input_uri"},
        )
        if isinstance(x, str) and x.strip()
    ]
    mapping = payload if isinstance(payload, Mapping) else {}
    worker = mapping.get("worker_loop") if isinstance(mapping.get("worker_loop"), Mapping) else {}
    required = mapping.get("required_outputs") if isinstance(mapping.get("required_outputs"), Mapping) else {}
    branches = mapping.get("branch_health") if isinstance(mapping.get("branch_health"), Mapping) else {}
    state_running = any(
        str(state).strip().lower() in {"playing", "running", "live", "on_air", "on-air"}
        for state in states
    )
    runtime_running = bool(mapping.get("running")) if "running" in mapping else state_running
    worker_running = bool(worker.get("running")) if worker else runtime_running
    required_outputs_healthy = (
        all(
            not bool(enabled)
            or bool(
                branches.get(
                    str(branch),
                    branches.get("icecast", False)
                    if str(branch).startswith("icecast:")
                    else False,
                )
            )
            for branch, enabled in required.items()
        )
        if required
        else runtime_running
    )
    recovery = mapping.get("recovery") if isinstance(mapping.get("recovery"), Mapping) else {}
    recovery_attempt_count = _number(recovery.get("attempt_count"))
    projection = {
        "states": sorted(set(states))[:20],
        "max_bytes": max((x for x in bytes_seen if x is not None), default=None),
        "max_reconnects": max((x for x in reconnects if x is not None), default=None),
        "encoder_error_count": max((x for x in encoder_errors if x is not None), default=None),
        "encoder_error_present": bool(encoder_error_lines),
        "recovery_attempt_count": recovery_attempt_count,
        "track_present": bool(tracks),
        "runtime_running": runtime_running,
        "worker_running": worker_running,
        "required_outputs_healthy": required_outputs_healthy,
        "runtime_count": len(payload) if isinstance(payload, list) else (1 if isinstance(payload, Mapping) else None),
    }
    projection["runtime_healthy"] = bool(
        projection["runtime_running"]
        and projection["worker_running"]
        and projection["required_outputs_healthy"]
        and projection["track_present"]
    )
    return projection


def _request_json(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, Any, str | None]:
    request = Request(url, headers={"Accept": "application/json", **dict(headers)})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(512 * 1024)
            try:
                return int(response.status), json.loads(raw.decode("utf-8")), None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return int(response.status), None, "invalid_json"
    except HTTPError as exc:
        return int(exc.code), None, "http_error"
    except (OSError, URLError, TimeoutError) as exc:
        return 0, None, type(exc).__name__


def _request_stream(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "audio/*", "Icy-MetaData": "1"})
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(64 * 1024)
            content_type = response.headers.get("Content-Type", "")
            return {
                "status": int(response.status),
                "content_type_audio": content_type.lower().startswith("audio/"),
                "bytes": len(data),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except HTTPError as exc:
        return {"status": int(exc.code), "bytes": 0, "error": "http_error"}
    except (OSError, URLError, TimeoutError) as exc:
        return {"status": 0, "bytes": 0, "error": type(exc).__name__}


def _login(config: Config, password: str) -> tuple[dict[str, str], dict[str, Any]]:
    body = json.dumps({"username": config.username, "password": password}).encode("utf-8")
    request = Request(
        f"{config.api_base}/api/auth/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read(128 * 1024).decode("utf-8"))
    except (HTTPError, OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoakError(f"API login failed ({type(exc).__name__})") from exc
    token = payload.get("access_token") if isinstance(payload, Mapping) else None
    if not isinstance(token, str) or not token:
        raise SoakError("API login returned no access token")
    return {"Authorization": f"Bearer {token}"}, {"login": "ok"}


def _sample(config: Config, headers: Mapping[str, str]) -> dict[str, Any]:
    health_status, health, health_error = _request_json(f"{config.api_base}/api/health", headers, config.timeout_seconds)
    station_status, stations, station_error = _request_json(f"{config.api_base}/api/stations", headers, config.timeout_seconds)
    # The unified API exposes a station-scoped runtime contract.  Keep a
    # read-only compatibility fallback for older installed builds that still
    # expose the retired fleet projection.
    runtime_status, runtimes, runtime_error = _request_json(
        f"{config.api_base}/api/runtime/{int(config.station_id)}/status",
        headers,
        config.timeout_seconds,
    )
    if runtime_status in {404, 405}:
        runtime_status, runtimes, runtime_error = _request_json(
            f"{config.api_base}/api/stations/runtimes",
            headers,
            config.timeout_seconds,
        )
    disk = shutil.disk_usage(config.output.parent)
    sample: dict[str, Any] = {
        "timestamp": _utc_now(),
        "api": {
            "health_status": health_status,
            "stations_status": station_status,
            "runtimes_status": runtime_status,
            "health_error": health_error,
            "stations_error": station_error,
            "runtimes_error": runtime_error,
            "health": _json_safe(health) if isinstance(health, Mapping) else None,
            "station_count": len(stations) if isinstance(stations, list) else None,
            "runtime": _runtime_projection(runtimes),
        },
        "disk": {"free_bytes": disk.free, "total_bytes": disk.total, "free_ratio": round(disk.free / disk.total, 6) if disk.total else 0},
    }
    if config.stream_url:
        sample["stream"] = _request_stream(config.stream_url, config.timeout_seconds)
    return sample


def _validate_args(args: argparse.Namespace) -> Config:
    api_base = _safe_url(args.api_base, "api_base", api=True)
    stream_url = _safe_url(args.stream_url, "stream_url") if args.stream_url else None
    if args.interval_seconds < 1 or args.interval_seconds > 3600:
        raise SoakError("interval_seconds must be between 1 and 3600")
    if args.duration_seconds <= 0 or args.duration_seconds > 31 * 24 * 3600:
        raise SoakError("duration_seconds must be between 1 second and 31 days")
    if args.once:
        duration = 1
    else:
        duration = args.duration_seconds
    output = _safe_file(args.output, "output", must_exist=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    return Config(
        api_base=api_base,
        username=args.username,
        password_file=_safe_file(args.password_file, "password_file", must_exist=True),
        stream_url=stream_url,
        station_id=args.station_id,
        interval_seconds=float(args.interval_seconds),
        duration_seconds=float(duration),
        output=output,
        timeout_seconds=float(args.timeout_seconds),
        once=bool(args.once),
    )


def run(config: Config) -> int:
    password = _read_password(config.password_file)
    headers, login_record = _login(config, password)
    # Drop the credential reference as early as possible.  It is never part of
    # a record, exception, or command-line echo.
    del password
    started = time.monotonic()
    samples = 0
    failures = 0
    max_reconnects: float | None = None
    max_bytes: float | None = None
    max_encoder_errors: float | None = None
    previous_recovery_attempts: float | None = None
    previous_encoder_errors: float | None = None
    recovery_attempt_increases = 0.0
    encoder_error_increases = 0.0
    counter_resets = 0
    runtime_unhealthy_samples = 0
    config.output.parent.mkdir(parents=True, exist_ok=True)
    with config.output.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "soak_start", "timestamp": _utc_now(), "config": {"api_base": config.api_base, "station_id": config.station_id, "interval_seconds": config.interval_seconds, "duration_seconds": config.duration_seconds, "stream_configured": bool(config.stream_url)}, "auth": login_record}, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        while True:
            sample = _sample(config, headers)
            samples += 1
            api = sample["api"]
            stream = sample.get("stream")
            projection = api.get("runtime", {})
            stability_ok = True
            if isinstance(projection, Mapping):
                if isinstance(projection.get("max_reconnects"), (int, float)):
                    max_reconnects = max(max_reconnects or 0, float(projection["max_reconnects"]))
                if isinstance(projection.get("max_bytes"), (int, float)):
                    max_bytes = max(max_bytes or 0, float(projection["max_bytes"]))
                if isinstance(projection.get("encoder_error_count"), (int, float)):
                    current_encoder_errors = float(projection["encoder_error_count"])
                    max_encoder_errors = max(max_encoder_errors or 0, current_encoder_errors)
                    if previous_encoder_errors is not None:
                        if current_encoder_errors < previous_encoder_errors:
                            counter_resets += 1
                            stability_ok = False
                        elif current_encoder_errors > previous_encoder_errors:
                            encoder_error_increases += current_encoder_errors - previous_encoder_errors
                            stability_ok = False
                    previous_encoder_errors = current_encoder_errors
                if isinstance(projection.get("recovery_attempt_count"), (int, float)):
                    current_recovery_attempts = float(projection["recovery_attempt_count"])
                    if previous_recovery_attempts is not None:
                        if current_recovery_attempts < previous_recovery_attempts:
                            counter_resets += 1
                            stability_ok = False
                        elif current_recovery_attempts > previous_recovery_attempts:
                            recovery_attempt_increases += current_recovery_attempts - previous_recovery_attempts
                            stability_ok = False
                    previous_recovery_attempts = current_recovery_attempts
                if projection.get("runtime_healthy") is not True:
                    runtime_unhealthy_samples += 1
            good_api = bool(
                all(api.get(key) == 200 for key in ("health_status", "stations_status", "runtimes_status"))
                and isinstance(projection, Mapping)
                and projection.get("runtime_healthy") is True
                and stability_ok
            )
            good_stream = stream is None or (stream.get("status") == 200 and stream.get("bytes", 0) > 0 and stream.get("content_type_audio") is True)
            if not (good_api and good_stream):
                failures += 1
            handle.write(json.dumps({"type": "sample", "ok": good_api and good_stream, **sample}, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if config.once or time.monotonic() - started >= config.duration_seconds:
                break
            time.sleep(min(config.interval_seconds, max(0.0, config.duration_seconds - (time.monotonic() - started))))
        summary = {"type": "soak_summary", "timestamp": _utc_now(), "samples": samples, "failures": failures, "max_reconnects": max_reconnects, "max_bytes": max_bytes, "max_encoder_errors": max_encoder_errors, "recovery_attempt_increases": recovery_attempt_increases, "encoder_error_increases": encoder_error_increases, "counter_resets": counter_resets, "runtime_unhealthy_samples": runtime_unhealthy_samples, "duration_seconds": round(time.monotonic() - started, 3), "passed": samples > 0 and failures == 0 and (config.once or time.monotonic() - started >= config.duration_seconds)}
        handle.write(json.dumps(summary, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({"samples": samples, "failures": failures, "passed": summary["passed"], "evidence": str(config.output)}, separators=(",", ":")))
    return 0 if summary["passed"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8100")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", default=r"C:\ProgramData\RadioTEDU\OnAir\initial-admin-password.txt")
    parser.add_argument("--stream-url")
    parser.add_argument("--station-id", type=int)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--duration-hours", type=float, default=24)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT / f"soak-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"))
    parser.add_argument("--once", action="store_true", help="collect one sample and finish (useful for commissioning)")
    args = parser.parse_args(argv)
    args.duration_seconds = args.duration_seconds if args.duration_seconds is not None else args.duration_hours * 3600
    try:
        return run(_validate_args(args))
    except SoakError as exc:
        print(f"Soak test refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
