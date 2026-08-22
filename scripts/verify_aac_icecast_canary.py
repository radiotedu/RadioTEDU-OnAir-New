from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify RadioTEDU AAC profiles on isolated private Icecast mounts."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--station-id", type=int, default=0)
    return parser


def _status_source(host: str, port: int, tls: bool, mount: str) -> dict:
    scheme = "https" if tls else "http"
    request = urllib.request.Request(
        f"{scheme}://{host}:{port}/status-json.xsl",
        headers={"User-Agent": "RadioTEDU-OnAir-Codec-Canary/1.0"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    sources = dict(payload.get("icestats") or {}).get("source") or []
    if isinstance(sources, dict):
        sources = [sources]
    for source in sources:
        listen_url = str(dict(source).get("listenurl") or "")
        source_mount = str(dict(source).get("mount") or "")
        if source_mount == mount or listen_url.rstrip().endswith(mount):
            return dict(source)
    return {}


def _run_profile(base, ffmpeg: str, profile: str, bitrate: int, fdk_profile: str) -> dict:
    from dataclasses import replace

    from app.audio.icecast_source_transport import IcecastSourceTransport

    suffix = uuid.uuid4().hex[:10]
    mount = f"/_radiotedu-canary-{bitrate}-{suffix}"
    cfg = replace(
        base,
        icecast_mount=mount,
        stream_codec_profile=profile,
        stream_bitrate_kbps=bitrate,
        icecast_public=False,
        icecast_stream_name="RadioTEDU private codec canary",
        icecast_description="Temporary private codec verification",
        icecast_genre="Test",
        metadata_suppressed=True,
    )
    transport = None
    process = None
    observed: dict = {}
    try:
        transport = IcecastSourceTransport(cfg)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000:duration=7",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "libfdk_aac",
            "-profile:a",
            fdk_profile,
            "-b:a",
            f"{bitrate}k",
            "-afterburner",
            "1",
            "-f",
            "adts",
            "pipe:1",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        started = time.monotonic()
        while process.stdout is not None:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            transport.send(chunk)
            if time.monotonic() - started >= 1.5:
                try:
                    observed = _status_source(
                        cfg.icecast_host,
                        int(cfg.icecast_port),
                        bool(cfg.icecast_tls_enabled),
                        mount,
                    )
                except Exception:
                    observed = {}
                if observed:
                    break
        observed_bitrate = int(float(observed.get("bitrate") or 0))
        return {
            "profile": profile,
            "expected_bitrate_kbps": bitrate,
            "observed_bitrate_kbps": observed_bitrate,
            "content_type": str(observed.get("server_type") or ""),
            "passed": observed_bitrate == bitrate,
        }
    finally:
        if transport is not None:
            transport.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)


def main() -> int:
    args = _parser().parse_args()
    database = Path(args.db).resolve()
    data_root = Path(args.data_root).resolve()
    ffmpeg = Path(args.ffmpeg).resolve()
    if not database.is_file() or not ffmpeg.is_file() or not data_root.is_dir():
        print(json.dumps({"ok": False, "error": "required_path_missing"}))
        return 2

    os.environ["CLEANROOM_DB_PATH"] = str(database)
    os.environ["CLEANROOM_DATA_ROOT"] = str(data_root)
    os.environ["CLEANROOM_USER_CONFIG_ROOT"] = str(data_root)
    os.environ["CLEANROOM_CREDENTIAL_STORE_FILE"] = str(
        data_root / "secrets" / "station-credentials.json"
    )
    os.environ["CLEANROOM_CREDENTIAL_DPAPI_SCOPE"] = "machine"
    os.environ["RADIOTEDU_FFMPEG_PATH"] = str(ffmpeg)

    from app.audio.gst_pipeline import StationPipelineConfig
    from app.db import get_connection
    from app.repositories.station_output_repo import StationOutputRepository

    conn = get_connection()
    try:
        if args.station_id > 0:
            station_ids = [args.station_id]
        else:
            station_ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT station_id FROM station_outputs "
                    "WHERE icecast_enabled=1 ORDER BY station_id"
                ).fetchall()
            ]
        selected = None
        for station_id in station_ids:
            candidate = StationOutputRepository(conn).get(station_id)
            if candidate and str(candidate.get("icecast_password") or ""):
                selected = candidate
                break
    finally:
        conn.close()
    if selected is None:
        print(json.dumps({"ok": False, "error": "source_credential_unavailable"}))
        return 2

    base = StationPipelineConfig(
        input_uri="silence://continuous",
        icecast_host=str(selected["icecast_host"]),
        icecast_port=int(selected["icecast_port"]),
        icecast_mount="/_radiotedu-canary",
        icecast_user=str(selected["icecast_user"]),
        icecast_password=str(selected["icecast_password"]),
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_tls_enabled=int(selected["icecast_port"]) == 443,
        icecast_public=False,
    )
    results = []
    for profile, bitrate, fdk_profile in (
        ("aac_low_192", 192, "aac_low"),
        ("aac_he_v2_64", 64, "aac_he_v2"),
    ):
        try:
            results.append(
                _run_profile(base, str(ffmpeg), profile, bitrate, fdk_profile)
            )
        except Exception as exc:
            results.append(
                {
                    "profile": profile,
                    "expected_bitrate_kbps": bitrate,
                    "observed_bitrate_kbps": 0,
                    "content_type": "",
                    "passed": False,
                    "error": type(exc).__name__,
                }
            )
    ok = all(item.get("passed") for item in results)
    print(json.dumps({"ok": ok, "results": results}, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
