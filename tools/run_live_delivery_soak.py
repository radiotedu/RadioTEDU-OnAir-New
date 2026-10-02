"""Verify persisted outputs, source continuity and decoded public audio together."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from tools import monitor_stream_continuity as monitor


COUNTERS = ("encoded_bytes_sent", "continuity_silence_chunks", "dropped_pcm_chunks", "encoder_error_count", "network_error_count")


def _enabled(value, default=True):
    return default if value is None else str(value).lower().strip() in {"true", "1", "yes", "on"}


def _same_codec_profile(expected: str, effective: str) -> bool:
    # Both names describe AAC LC at 192 kbps. Decoder evidence separately
    # validates the received profile and bitrate, including any fallback.
    aliases = {"aac_lc_192": "aac_low_192"}
    return aliases.get(expected, expected) == aliases.get(effective, effective)


def capture_roster(database: Path) -> list[dict]:
    """Read only public output fields; never read source credentials."""
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = conn.execute("SELECT station_id,icecast_host,icecast_port,icecast_mount,stream_codec_profile,stream_bitrate_kbps FROM station_outputs WHERE icecast_enabled=1 ORDER BY station_id").fetchall()
        result = []
        for row in rows:
            sid = int(row["station_id"])
            key = f"station_{sid}_extra_icecast_outputs"
            setting = conn.execute("SELECT value FROM station_settings WHERE station_id=? AND key=?", (sid, key)).fetchone()
            if setting is None:
                setting = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
            extras = json.loads(setting[0] or "[]") if setting is not None else []
            if not isinstance(extras, list):
                raise RuntimeError(f"station {sid}: invalid persisted output list")
            for primary, item in [(True, dict(row))] + [(False, v) for v in extras if isinstance(v, dict) and _enabled(v.get("enabled"))]:
                host = str(item.get("icecast_host") or item.get("host") or row["icecast_host"])
                port = int(item.get("icecast_port", item.get("port", row["icecast_port"])))
                mount = str(item.get("icecast_mount") or item.get("mount") or "")
                if not mount.startswith("/"):
                    mount = "/" + mount
                label = mount.lstrip("/")
                url = f"http://{host}:{port}{mount}"
                monitor._parse_stream_arg(f"{label}={url}")
                profile = str(item.get("stream_codec_profile") or item.get("codec") or row["stream_codec_profile"])
                bitrate = int(item.get("stream_bitrate_kbps", item.get("bitrate_kbps", row["stream_bitrate_kbps"])))
                if not primary and mount.endswith("-low"):
                    profile, bitrate = "aac_he_v2_64", 64
                elif not primary and mount.endswith("-high"):
                    profile, bitrate = "aac_low_192", 192
                result.append({"station_id": sid, "primary": primary, "label": label, "url": url, "mount": mount, "codec_profile": profile, "bitrate_kbps": bitrate})
    if not result or len({r["label"] for r in result}) != len(result):
        raise RuntimeError("persisted enabled output roster is empty or has duplicates")
    return sorted(result, key=lambda row: row["label"])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() != 5:  # ERROR_ACCESS_DENIED
                return False
            # Supervisor workers run as SYSTEM. A desktop user's process query
            # handle can be denied even though the PID is running. EnumProcesses
            # can enumerate those PIDs without opening or changing a process.
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.EnumProcesses.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            psapi.EnumProcesses.restype = ctypes.c_int
            capacity = 4096
            while capacity <= 65536:
                pids = (ctypes.c_uint32 * capacity)()
                returned = ctypes.c_uint32()
                if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
                    return False
                count = returned.value // ctypes.sizeof(ctypes.c_uint32)
                if count < capacity:
                    return pid in pids[:count]
                capacity *= 2
            return False
        try:
            exit_code = ctypes.c_uint32()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sample_sources(data_root: Path, roster: list[dict]) -> dict:
    heartbeats = {}
    result = {}
    for row in roster:
        sid = row["station_id"]
        try:
            if sid not in heartbeats:
                value = json.loads((data_root / "State" / "StationWorkers" / f"station-{sid}.heartbeat.json").read_text(encoding="utf-8"))
                pid = int(value.get("pid") or value.get("worker_pid") or 0)
                value["process_alive"] = _pid_alive(pid)
                heartbeats[sid] = value
            h = heartbeats[sid]
            r = h.get("runtime_status") or {}
            health = r.get("icecast_mount_health") if row["primary"] else next((item.get("health") for item in r.get("extra_icecast_mounts") or [] if (item.get("mount") or item.get("icecast_mount")) == row["mount"]), None)
            health = health or {}
            tick_age = h.get("scheduler_tick_age_seconds")
            network_age = health.get("last_network_write_age_seconds")
            conditions = {
                "process_alive": h["process_alive"],
                "worker_running": h.get("running"),
                "fresh_heartbeat": 0 <= time.time() - float(h.get("updated_epoch") or 0) <= 5,
                "fresh_scheduler": tick_age is not None and 0 <= float(tick_age) <= 5,
                "program_running": r.get("program_running"),
                "pcm_advancing": not r.get("program_pcm_stalled"),
                "encoder_running": health.get("process_running"),
                "writer_running": health.get("writer_running"),
                "source_healthy": health.get("mount_healthy") and not health.get("network_failed"),
                "network_advancing": network_age is not None and float(network_age) <= 5,
                "codec_contract": _same_codec_profile(row["codec_profile"], str(health.get("effective_stream_codec_profile") or "")),
            }
            result[row["label"]] = {"ready": all(conditions.values()), "not_ready_reasons": [name for name, value in conditions.items() if not value], "pid": h.get("pid") or h.get("worker_pid"), "generation": h.get("generation"), "counters": {k: int(health.get(k) or 0) for k in COUNTERS}}
        except (OSError, ValueError, TypeError, StopIteration) as exc:
            result[row["label"]] = {"ready": False, "error": type(exc).__name__}
    return result


def source_changes(before: dict, after: dict) -> list[str]:
    issues = []
    if set(before) != set(after):
        return ["runtime output roster changed"]
    for label, current in after.items():
        old = before[label]
        if not current.get("ready"):
            issues.append(f"{label}: source not ready")
        if (current.get("pid"), current.get("generation")) != (old.get("pid"), old.get("generation")):
            issues.append(f"{label}: worker restarted")
        for key in COUNTERS:
            value = current.get("counters", {}).get(key)
            previous = old.get("counters", {}).get(key)
            if value is None or previous is None or value < previous:
                issues.append(f"{label}: {key} reset or missing")
            elif key != "encoded_bytes_sent" and value != previous:
                issues.append(f"{label}: {key} increased")
    return issues


def _save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def codec_contract_errors(roster: list[dict], streams: dict) -> list[str]:
    errors = []
    for row in roster:
        description = str(streams.get(row["label"], {}).get("input_audio_description") or "")
        expected = {"aac_low_192": "aac (LC)", "aac_he_v2_64": "aac (HE-AACv2)", "ogg_flac_lossless": "flac"}.get(row["codec_profile"])
        if expected is None or not description.lower().startswith(expected.lower()):
            errors.append(f"{row['label']}: decoded input codec does not match {row['codec_profile']}")
        bitrate = re.search(r"([0-9.]+) kb/s", description)
        target = row["bitrate_kbps"]
        if target > 0 and (bitrate is None or not target * 0.85 <= float(bitrate[1]) <= target * 1.15):
            errors.append(f"{row['label']}: decoded input bitrate is missing or outside contract")
    return errors


def run(args) -> int:
    data_root = Path(args.data_root)
    database = data_root / "cleanroom.db"
    roster = capture_roster(database)
    streams = [(row["label"], row["url"]) for row in roster]
    # Existing decoder verifier pins its code roster. Check actual DB authority
    # independently, refusing any output it cannot cover rather than omitting it.
    canonical, _ = monitor._canonical_stream_roster()
    if dict(streams) != canonical:
        raise RuntimeError("enabled live outputs differ from decoder coverage: " + monitor._roster_difference(canonical, dict(streams)))
    output_dir = data_root / "Diagnostics" / "live-delivery-soak" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=False)
    state_path = output_dir / "state.json"
    _save(data_root / "Diagnostics" / "live-delivery-soak" / "latest.json", {"pid": os.getpid(), "directory": str(output_dir), "state": str(state_path)})
    stable_since = None
    startup_failure = None
    previous = None
    deadline = time.monotonic() + args.startup_timeout_seconds
    next_report = 0.0
    while True:
        now = time.monotonic()
        current = sample_sources(data_root, roster)
        if all(row.get("ready") for row in current.values()) and (previous is None or not source_changes(previous, current)):
            stable_since = now if stable_since is None else stable_since
        else:
            stable_since = None
        if now >= next_report:
            _save(state_path, {"phase": "waiting_for_stable_sources", "pid": os.getpid(), "updated_epoch": time.time(), "sources": current})
            next_report = now + 30
        previous = current
        if stable_since is not None and now - stable_since >= 30:
            break
        if now >= deadline:
            _save(state_path, {"phase": "readiness_failed", "pid": os.getpid(), "updated_epoch": time.time(), "sources": current})
            if not args.observe_unready:
                return 2
            # Keep collecting every output when an origin is already failing.
            # This observation can never certify a clean uninterrupted run.
            startup_failure = "source readiness deadline exceeded"
            break
        time.sleep(1)
    baseline = current
    manifest = output_dir / "live-roster.json"
    _save(manifest, {"schema_version": 1, "source": str(database), "captured_at_utc": datetime.now(UTC).isoformat(), "streams": roster})
    stop = threading.Event()
    issues = {startup_failure: time.time()} if startup_failure else {}
    issues_lock = threading.Lock()
    started_epoch = time.time()

    def watch():
        next_report = 0.0
        while not stop.wait(1):
            try:
                current = sample_sources(data_root, roster)
                failures = source_changes(baseline, current)
                if time.monotonic() >= next_report:
                    if capture_roster(database) != roster:
                        failures.append("persisted output configuration changed")
                    next_report = time.monotonic() + 30
                    with issues_lock:
                        for issue in failures:
                            issues.setdefault(issue, time.time())
                        _save(state_path, {"phase": "measuring", "pid": os.getpid(), "updated_epoch": time.time(), "started_epoch": started_epoch, "roster_count": len(roster), "source_issues": dict(issues), "sources": current})
                else:
                    with issues_lock:
                        for issue in failures:
                            issues.setdefault(issue, time.time())
            except Exception as exc:
                with issues_lock:
                    issues.setdefault(f"observer failed: {type(exc).__name__}", time.time())

    thread = threading.Thread(target=watch, name="source-continuity-observer", daemon=True)
    thread.start()
    output = output_dir / "decoded-delivery.jsonl"
    argv = ["--ffmpeg", args.ffmpeg, "--expected-roster-file", str(manifest), "--duration-seconds", str(args.duration_seconds), "--sample-seconds", "10", "--listener-buffer-seconds", "4", "--minimum-margin-seconds", "0", "--maximum-decoded-audio-gap-seconds", "4", "--maximum-progress-age-seconds", "4", "--warmup-seconds", "20", "--readiness-stable-seconds", "20", "--output", str(output)]
    for label, url in streams:
        argv.extend(["--stream", f"{label}={url}"])
    try:
        code = monitor.main(argv)
    finally:
        stop.set()
        thread.join(timeout=5)
    listener_summary = json.loads(output.with_suffix(output.suffix + ".summary.json").read_text(encoding="utf-8"))
    final_sources = sample_sources(data_root, roster)
    for issue in source_changes(baseline, final_sources):
        issues.setdefault(issue, time.time())
    current_roster_match = capture_roster(database) == roster
    for issue in codec_contract_errors(roster, listener_summary.get("streams") or {}):
        issues.setdefault(issue, time.time())
    verified = bool(code == 0 and not issues and current_roster_match and listener_summary.get("duration_boundary_sampled") and float(listener_summary.get("measured_duration_seconds") or 0) >= args.duration_seconds)
    _save(state_path, {"phase": "passed" if verified else "failed", "pid": os.getpid(), "updated_epoch": time.time(), "live_runtime_roster_authoritative": True, "roster": roster, "source_issues": issues, "decoded_summary": str(output.with_suffix(output.suffix + ".summary.json")), "requested_duration_seconds": args.duration_seconds, "verified": verified})
    return 0 if verified else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=r"C:\ProgramData\RadioTEDU\OnAir")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--duration-seconds", type=float, default=86400)
    parser.add_argument("--startup-timeout-seconds", type=float, default=900)
    parser.add_argument("--inspect-roster", action="store_true")
    parser.add_argument("--observe-unready", action="store_true", help="Continue collecting after startup failure; the run remains failed.")
    args = parser.parse_args()
    if not 10 <= args.duration_seconds <= 604800:
        parser.error("duration-seconds must be between 10 and 604800")
    if args.inspect_roster:
        roster = capture_roster(Path(args.data_root) / "cleanroom.db")
        statuses = sample_sources(Path(args.data_root), roster)
        print(json.dumps({"outputs": len(roster), "stations": len({r["station_id"] for r in roster}), "waiting": {label: row.get("not_ready_reasons") for label, row in statuses.items() if not row["ready"]}}))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
