"""Start one station and report only after the public stream is verified live."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "RadioTEDU" / "OnAir"
os.environ.setdefault("CLEANROOM_DB_PATH", str(DATA_ROOT / "cleanroom.db"))
os.environ.setdefault("CLEANROOM_DATA_ROOT", str(DATA_ROOT))
os.environ.setdefault("CLEANROOM_USER_CONFIG_ROOT", str(DATA_ROOT))
os.environ.setdefault(
    "CLEANROOM_JWT_SECRET_FILE",
    str(Path(os.environ.get("LOCALAPPDATA", str(DATA_ROOT.parent))) / "RadioTEDU" / "OnAir" / "secrets" / "jwt-signing.key"),
)
sys.path.insert(0, str(ROOT))

from app.auth.jwt_handler import create_access_token  # noqa: E402


def _request(base_url: str, method: str, path: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    response = requests.request(
        method,
        base_url.rstrip("/") + path,
        headers={"Authorization": "Bearer " + create_access_token(1, "admin")},
        json=payload,
        timeout=timeout,
    )
    if not response.ok:
        detail = " ".join(str(response.text or "").split())[:500]
        raise RuntimeError(f"API {method} {path} failed with HTTP {response.status_code}: {detail}")
    body = response.json()
    return dict(body) if isinstance(body, dict) else {"data": body}


def _origin_probe(output: dict) -> dict:
    scheme = "https" if bool(output.get("icecast_tls_enabled")) else "http"
    host = str(output.get("icecast_host") or "").strip()
    port = int(output.get("icecast_port") or 0)
    mount = str(output.get("icecast_mount") or "").strip()
    if mount and not mount.startswith("/"):
        mount = f"/{mount}"
    result = {}
    for label, path in (("root", "/"), ("mount", mount or "/")):
        try:
            response = requests.get(
                f"{scheme}://{host}:{port}{path}",
                timeout=3.0,
                stream=True,
                allow_redirects=False,
            )
            result[label] = {
                "status": response.status_code,
                "content_type": str(response.headers.get("content-type") or ""),
                "server": str(response.headers.get("server") or ""),
            }
            response.close()
        except requests.RequestException as exc:
            result[label] = {"status": 0, "error": type(exc).__name__}
    return result


def _public_listener_probe(base_url: str, mount: str) -> dict:
    base = str(base_url or "").strip().rstrip("/")
    normalized_mount = str(mount or "").strip()
    if normalized_mount and not normalized_mount.startswith("/"):
        normalized_mount = f"/{normalized_mount}"
    if not base or not normalized_mount:
        return {"status": 0, "error": "not_configured"}
    try:
        response = requests.get(
            f"{base}{normalized_mount}",
            timeout=3.0,
            stream=True,
            allow_redirects=False,
        )
        content_type = str(response.headers.get("content-type") or "")
        sample_bytes = 0
        if response.status_code in {200, 206} and content_type.lower().startswith("audio/"):
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    sample_bytes = len(chunk)
                    break
        result = {
            "status": response.status_code,
            "content_type": content_type,
            "server": str(response.headers.get("server") or ""),
            "sample_bytes": sample_bytes,
        }
        response.close()
        return result
    except requests.RequestException as exc:
        return {"status": 0, "error": type(exc).__name__}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("station_id", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--enable-autostart", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--restart-runtime", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--stable-seconds", type=float, default=5.0)
    parser.add_argument("--direct-output-check", action="store_true")
    parser.add_argument("--api-output-check", action="store_true")
    parser.add_argument("--public-check", action="store_true")
    parser.add_argument("--auth-check", action="store_true")
    parser.add_argument("--runtime-check", action="store_true")
    args = parser.parse_args()

    if args.auth_check:
        payload = _request(args.base_url, "GET", "/api/auth/me")
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_id": payload.get("id"),
                    "role": payload.get("role"),
                    "permission_count": len(payload.get("permissions") or []),
                },
                separators=(",", ":"),
            )
        )
        return 0

    if args.runtime_check:
        payload = _request(
            args.base_url,
            "GET",
            f"/api/runtime/{args.station_id}/status",
        )
        worker = payload.get("worker_loop") or {}
        print(
            json.dumps(
                {
                    "ok": True,
                    "station_id": args.station_id,
                    "running": bool(payload.get("running")),
                    "branch_health": payload.get("branch_health") or {},
                    "required_outputs": payload.get("required_outputs") or {},
                    "worker_running": bool(worker.get("running")),
                    "worker_last_error": str(worker.get("last_error") or "")[:500],
                    "worker_last_result": worker.get("last_result") or {},
                    "recovery": payload.get("recovery") or {},
                },
                separators=(",", ":"),
            )
        )
        return 0

    if args.public_check:
        payload = _request(args.base_url, "GET", "/api/public/stations")
        station = next(
            (
                item
                for item in payload.get("stations", [])
                if int(item.get("id") or 0) == args.station_id
            ),
            {},
        )
        print(
            json.dumps(
                {
                    "ok": bool(station),
                    "station_id": args.station_id,
                    "status": station.get("status"),
                    "status_reason": station.get("status_reason"),
                    "has_now_playing": station.get("now_playing") is not None,
                    "has_preserved_item": station.get("preserved_item") is not None,
                },
                separators=(",", ":"),
            )
        )
        return 0 if station else 1

    if args.direct_output_check:
        try:
            from app.api.stations import get_station_output

            output = get_station_output(args.station_id, _user={})
            print(
                json.dumps(
                    {
                        "ok": True,
                        "station_id": args.station_id,
                        "icecast_host": output.get("icecast_host"),
                        "icecast_port": output.get("icecast_port"),
                        "icecast_mount": output.get("icecast_mount"),
                        "icecast_password_configured": output.get("icecast_password_configured"),
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        except Exception as exc:
            print(
                json.dumps(
                    {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]},
                    separators=(",", ":"),
                )
            )
            return 1

    if args.api_output_check:
        output = _request(
            args.base_url,
            "GET",
            f"/api/stations/output?station_id={args.station_id}",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "station_id": args.station_id,
                    "icecast_host": output.get("icecast_host"),
                    "icecast_port": output.get("icecast_port"),
                    "icecast_mount": output.get("icecast_mount"),
                    "icecast_password_configured": output.get("icecast_password_configured"),
                },
                separators=(",", ":"),
            )
        )
        return 0

    if args.enable_autostart and not args.verify_only:
        _request(
            args.base_url,
            "POST",
            "/api/settings/station",
            {"station_id": args.station_id, "broadcast_autostart_enabled": "true"},
        )

    if args.restart_runtime and not args.verify_only:
        _request(
            args.base_url,
            "POST",
            f"/api/runtime/{args.station_id}/operator-stop",
            timeout=30.0,
        )
        time.sleep(0.5)

    if not args.verify_only:
        _request(
            args.base_url,
            "POST",
            f"/api/runtime/{args.station_id}/operator-start",
            {"fallback_uri": "", "interval_sec": 1},
            timeout=45.0,
        )

    output = _request(
        args.base_url,
        "GET",
        f"/api/stations/output?station_id={args.station_id}",
    )
    system_settings = _request(args.base_url, "GET", "/api/settings/system")
    station_settings_payload = _request(
        args.base_url,
        "GET",
        f"/api/settings/station?station_id={args.station_id}",
    )
    station_settings = station_settings_payload.get("settings") or station_settings_payload
    autostart_enabled = _truthy(station_settings.get("broadcast_autostart_enabled"))
    public_stream_base_url = str(
        (system_settings.get("settings") or system_settings).get("stream_public_base_url") or ""
    )
    deadline = time.monotonic() + max(1.0, args.timeout)
    last_runtime: dict = {}
    last_public: dict = {}
    public_listener: dict = {}
    verified_since: float | None = None
    while time.monotonic() < deadline:
        last_runtime = _request(args.base_url, "GET", f"/api/runtime/{args.station_id}/status")
        public_payload = _request(args.base_url, "GET", "/api/public/stations")
        last_public = next(
            (
                station
                for station in public_payload.get("stations", [])
                if int(station.get("id") or 0) == args.station_id
            ),
            {},
        )
        public_listener = _public_listener_probe(
            public_stream_base_url,
            str(output.get("icecast_mount") or ""),
        )
        public_listener_live = (
            int(public_listener.get("status") or 0) in {200, 206}
            and str(public_listener.get("content_type") or "").lower().startswith("audio/")
            and int(public_listener.get("sample_bytes") or 0) > 0
        )
        public_verified = (
            public_listener_live
            if public_stream_base_url.strip()
            else str(last_public.get("status") or "").lower() == "live"
        )
        sample_verified = (
            bool(last_runtime.get("running"))
            and bool((last_runtime.get("worker_loop") or {}).get("running"))
            and public_verified
        )
        if sample_verified:
            verified_since = verified_since or time.monotonic()
        else:
            verified_since = None
        if verified_since is not None and time.monotonic() - verified_since >= max(0.0, args.stable_seconds):
            print(
                json.dumps(
                    {
                        "ok": True,
                        "station_id": args.station_id,
                        "autostart_enabled": autostart_enabled,
                        "public_status": last_public.get("status"),
                        "status_reason": last_public.get("status_reason"),
                        "runtime_running": True,
                        "worker_running": True,
                        "branch_health": last_runtime.get("branch_health") or {},
                        "public_listener": public_listener,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        time.sleep(1.0)

    print(
        json.dumps(
            {
                "ok": False,
                "station_id": args.station_id,
                "autostart_enabled": autostart_enabled,
                "public_status": last_public.get("status"),
                "status_reason": last_public.get("status_reason"),
                "runtime_running": bool(last_runtime.get("running")),
                "worker_running": bool((last_runtime.get("worker_loop") or {}).get("running")),
                "branch_health": last_runtime.get("branch_health") or {},
                "recovery": last_runtime.get("recovery") or {},
                "icecast": {
                    "host": output.get("icecast_host"),
                    "port": output.get("icecast_port"),
                    "mount": output.get("icecast_mount"),
                    "user": output.get("icecast_user"),
                    "password_configured": output.get("icecast_password_configured"),
                    "tls_enabled": output.get("icecast_tls_enabled"),
                },
                "origin_probe": _origin_probe(output),
                "public_stream_base_url": public_stream_base_url,
                "public_listener": public_listener,
            },
            separators=(",", ":"),
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
