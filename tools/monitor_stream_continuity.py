from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit


DEFAULT_STREAMS = (
    ("cazz-flac", "http://stream.radiotedu.com:11154/cazz-flac"),
    ("cazz-low", "http://stream.radiotedu.com:11154/cazz-low"),
    ("cazz", "http://stream.radiotedu.com:11154/cazz"),
    ("classic-flac", "http://stream.radiotedu.com:11154/classic-flac"),
    ("classic-low", "http://stream.radiotedu.com:11154/classic-low"),
    ("classic", "http://stream.radiotedu.com:11154/classic"),
    ("energize-low", "http://stream.radiotedu.com:11154/energize-low"),
    ("energize", "http://stream.radiotedu.com:11154/energize"),
    ("lofi-low", "http://stream.radiotedu.com:11154/lofi-low"),
    ("lofi", "http://stream.radiotedu.com:11154/lofi"),
    ("maincharacter", "http://stream.radiotedu.com:11154/maincharacter"),
    ("radio-low", "http://stream.radiotedu.com:11154/radio-low"),
    ("radio", "http://stream.radiotedu.com:11154/radio"),
    ("rock-low", "http://stream.radiotedu.com:11154/rock-low"),
    ("rock", "http://stream.radiotedu.com:11154/rock"),
    ("situation", "http://stream.radiotedu.com:11154/situation"),
)
_CLOCK = re.compile(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$")
_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(
    r"silence_end:\s*([0-9.]+).*?silence_duration:\s*([0-9.]+)"
)
_TRANSPORT_ERROR = re.compile(
    r"connection reset|connection refused|timed out|server returned|"
    r"input/output error|end of file|http error|broken pipe|error while decoding",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_clock(value: str) -> float | None:
    match = _CLOCK.match(value.strip())
    if not match:
        return None
    return (
        float(match.group(1)) * 3600.0
        + float(match.group(2)) * 60.0
        + float(match.group(3))
    )


def _safe_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("stream URL must use http or https")
    if parsed.username or parsed.password or parsed.fragment:
        raise argparse.ArgumentTypeError("stream URL must not contain credentials or fragments")
    return value.strip()


def _parse_stream_arg(value: str) -> tuple[str, str]:
    label, separator, url = value.partition("=")
    label = label.strip()
    if not separator or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}", label):
        raise argparse.ArgumentTypeError("stream must use label=http(s)://URL")
    return label, _safe_url(url)


def build_ffmpeg_command(ffmpeg: Path, url: str) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        "-stats_period",
        "1",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_at_eof",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-threads",
        "1",
        "-i",
        url,
        "-vn",
        "-af",
        # A two-second detector hides precisely the short interruptions this
        # monitor is intended to find.  One PCM frame is about 21 ms.
        "silencedetect=noise=-75dB:d=0.01",
        "-f",
        "null",
        "NUL" if os.name == "nt" else "/dev/null",
        "-progress",
        "pipe:1",
    ]


@dataclass
class StreamState:
    label: str
    url: str
    process: subprocess.Popen[str]
    started_monotonic: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    media_seconds: float = 0.0
    first_progress_monotonic: float | None = None
    first_media_seconds: float = 0.0
    last_progress_monotonic: float | None = None
    max_progress_gap_seconds: float = 0.0
    measurement_started_monotonic: float | None = None
    measurement_media_baseline: float = 0.0
    measurement_last_progress_monotonic: float | None = None
    measurement_max_progress_gap_seconds: float = 0.0
    startup_delay_seconds: float | None = None
    startup_progress_age_seconds: float | None = None
    startup_transport_errors: int = 0
    readiness_satisfied: bool = False
    current_silence_started_at: float | None = None
    max_silence_seconds: float = 0.0
    total_silence_seconds: float = 0.0
    silence_events: int = 0
    transport_errors: int = 0
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    unexpected_exit_code: int | None = None


def _read_progress(state: StreamState, stream: IO[str]) -> None:
    for raw_line in iter(stream.readline, ""):
        line = raw_line.strip()
        if not line.startswith("out_time="):
            continue
        media_seconds = _parse_clock(line.partition("=")[2])
        if media_seconds is None:
            continue
        now = time.monotonic()
        with state.lock:
            if media_seconds <= state.media_seconds + 0.001:
                # FFmpeg emits progress records on a timer even while decoded
                # media time is frozen. Such records must not hide a real stall.
                continue
            if state.first_progress_monotonic is None:
                state.first_progress_monotonic = now
                state.first_media_seconds = media_seconds
            if state.last_progress_monotonic is not None:
                state.max_progress_gap_seconds = max(
                    state.max_progress_gap_seconds,
                    now - state.last_progress_monotonic,
                )
            if state.measurement_started_monotonic is not None:
                measurement_previous = (
                    state.measurement_last_progress_monotonic
                    or state.measurement_started_monotonic
                )
                state.measurement_max_progress_gap_seconds = max(
                    state.measurement_max_progress_gap_seconds,
                    now - measurement_previous,
                )
                state.measurement_last_progress_monotonic = now
            state.last_progress_monotonic = now
            state.media_seconds = max(state.media_seconds, media_seconds)


def _read_diagnostics(state: StreamState, stream: IO[str]) -> None:
    for raw_line in iter(stream.readline, ""):
        line = raw_line.strip()
        if not line:
            continue
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        with state.lock:
            if start:
                state.current_silence_started_at = float(start.group(1))
            if end:
                duration = float(end.group(2))
                if (
                    state.measurement_started_monotonic is not None
                    and state.current_silence_started_at is not None
                ):
                    silence_end = state.current_silence_started_at + duration
                    silence_start = max(
                        state.current_silence_started_at,
                        state.measurement_media_baseline,
                    )
                    duration = max(0.0, silence_end - silence_start)
                state.max_silence_seconds = max(state.max_silence_seconds, duration)
                state.total_silence_seconds += max(0.0, duration)
                state.silence_events += 1
                state.current_silence_started_at = None
            if _TRANSPORT_ERROR.search(line):
                state.transport_errors += 1
                state.stderr_tail.append(line[:300])


def _snapshot(state: StreamState, now: float, *, stopping: bool = False) -> dict[str, Any]:
    with state.lock:
        measurement_started = state.measurement_started_monotonic
        measuring = measurement_started is not None
        elapsed = (
            now - measurement_started
            if measuring
            else now - state.started_monotonic
        )
        media_seconds = state.media_seconds
        current_silence = 0.0
        if state.current_silence_started_at is not None:
            silence_start = state.current_silence_started_at
            if measuring:
                silence_start = max(silence_start, state.measurement_media_baseline)
            current_silence = max(0.0, media_seconds - silence_start)
        if measuring:
            measurement_last = (
                state.measurement_last_progress_monotonic
                or measurement_started
            )
            playback_margin = media_seconds - (
                state.measurement_media_baseline + elapsed
            )
            progress_age = now - (
                state.last_progress_monotonic or state.started_monotonic
            )
            maximum_progress_gap = max(
                state.measurement_max_progress_gap_seconds,
                now - (state.measurement_last_progress_monotonic or measurement_started),
            )
        elif state.first_progress_monotonic is None:
            playback_margin = None
            progress_age = now - state.started_monotonic
            maximum_progress_gap = state.max_progress_gap_seconds
        else:
            expected_media = state.first_media_seconds + (
                now - state.first_progress_monotonic
            )
            playback_margin = media_seconds - expected_media
            progress_age = now - (
                state.last_progress_monotonic or state.first_progress_monotonic
            )
            maximum_progress_gap = state.max_progress_gap_seconds
        maximum_silence = max(state.max_silence_seconds, current_silence)
        return {
            "label": state.label,
            "process_alive": state.process.poll() is None,
            "exit_code": state.process.poll(),
            "elapsed_seconds": round(elapsed, 3),
            "measurement_active": measuring,
            "startup_delay_seconds": (
                round(state.startup_delay_seconds, 3)
                if state.startup_delay_seconds is not None
                else None
            ),
            "startup_progress_age_seconds": (
                round(state.startup_progress_age_seconds, 3)
                if state.startup_progress_age_seconds is not None
                else None
            ),
            "startup_transport_errors": state.startup_transport_errors,
            "readiness_satisfied": state.readiness_satisfied,
            "media_seconds": round(media_seconds, 3),
            "playback_margin_seconds": (
                round(playback_margin, 3) if playback_margin is not None else None
            ),
            "progress_age_seconds": round(progress_age, 3),
            "max_progress_gap_seconds": round(maximum_progress_gap, 3),
            "current_silence_seconds": round(current_silence, 3),
            "max_silence_seconds": round(maximum_silence, 3),
            "total_silence_seconds": round(
                state.total_silence_seconds + current_silence, 3
            ),
            "silence_events": state.silence_events,
            "transport_errors": state.transport_errors,
            "diagnostic_tail": list(state.stderr_tail),
            "unexpected_exit": bool(state.process.poll() is not None and not stopping),
        }


def _start_stream(ffmpeg: Path, label: str, url: str) -> StreamState:
    process = subprocess.Popen(
        build_ffmpeg_command(ffmpeg, url),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None and process.stderr is not None
    state = StreamState(label, url, process, time.monotonic())
    threading.Thread(
        target=_read_progress,
        args=(state, process.stdout),
        name=f"continuity-progress-{label}",
        daemon=True,
    ).start()
    threading.Thread(
        target=_read_diagnostics,
        args=(state, process.stderr),
        name=f"continuity-diagnostics-{label}",
        daemon=True,
    ).start()
    return state


def _begin_measurement(
    state: StreamState,
    now: float,
    *,
    readiness_satisfied: bool = True,
) -> dict[str, Any]:
    """Separate connection acquisition from the steady-state measurement window."""
    with state.lock:
        state.startup_delay_seconds = (
            state.first_progress_monotonic - state.started_monotonic
            if state.first_progress_monotonic is not None
            else None
        )
        state.startup_progress_age_seconds = (
            now - state.last_progress_monotonic
            if state.last_progress_monotonic is not None
            else now - state.started_monotonic
        )
        state.startup_transport_errors = state.transport_errors
        state.readiness_satisfied = readiness_satisfied
        state.measurement_started_monotonic = now
        state.measurement_media_baseline = state.media_seconds
        state.measurement_last_progress_monotonic = state.last_progress_monotonic
        state.measurement_max_progress_gap_seconds = 0.0
        state.max_silence_seconds = 0.0
        state.total_silence_seconds = 0.0
        state.silence_events = 0
        state.transport_errors = 0
        return {
            "label": state.label,
            "first_advancing_media_seconds": round(state.first_media_seconds, 3),
            "startup_delay_seconds": (
                round(state.startup_delay_seconds, 3)
                if state.startup_delay_seconds is not None
                else None
            ),
            "startup_progress_age_seconds": round(
                state.startup_progress_age_seconds, 3
            ),
            "startup_transport_errors": state.startup_transport_errors,
            "readiness_satisfied": state.readiness_satisfied,
            "process_alive": state.process.poll() is None,
        }


def _evaluate(
    snapshots: list[dict[str, Any]],
    *,
    minimum_margin_seconds: float,
    maximum_progress_age_seconds: float,
    maximum_silence_seconds: float,
    maximum_progress_gap_seconds: float = 2.0,
    maximum_total_silence_seconds: float = 0.5,
    maximum_startup_delay_seconds: float = 60.0,
) -> dict[str, Any]:
    measured = [row for row in snapshots if row.get("measurement_active", True)]
    # Older saved fixtures and reports have no startup fields. Keep their old
    # behavior while new runs require a successful acquisition window.
    has_startup_metrics = any("startup_delay_seconds" in row for row in snapshots)
    startup_rows = [row for row in measured if "startup_delay_seconds" in row]
    startup_delay = max(
        (
            float(row["startup_delay_seconds"])
            for row in startup_rows
            if row["startup_delay_seconds"] is not None
        ),
        default=None,
    )
    startup_age = max(
        (float(row.get("startup_progress_age_seconds") or 0.0) for row in startup_rows),
        default=0.0,
    )
    startup_transport_errors = max(
        (int(row.get("startup_transport_errors", 0)) for row in startup_rows),
        default=0,
    )
    startup_ok = not has_startup_metrics or (
        startup_delay is not None
        and startup_delay <= maximum_startup_delay_seconds
        and startup_age <= maximum_progress_age_seconds
        and startup_transport_errors == 0
        and all(bool(row.get("readiness_satisfied", False)) for row in startup_rows)
    )
    minimum_margin = min(
        (
            float(row["playback_margin_seconds"])
            for row in measured
            if row["playback_margin_seconds"] is not None
        ),
        default=None,
    )
    maximum_progress_age = max(
        (float(row["progress_age_seconds"]) for row in measured),
        default=0.0,
    )
    maximum_progress_gap = max(
        (
            float(row.get("max_progress_gap_seconds", 0.0))
            for row in measured
        ),
        default=0.0,
    )
    maximum_silence = max(
        (float(row["max_silence_seconds"]) for row in measured),
        default=0.0,
    )
    maximum_total_silence = max(
        (float(row.get("total_silence_seconds", 0.0)) for row in measured),
        default=0.0,
    )
    unexpected_exit = any(bool(row["unexpected_exit"]) for row in measured)
    exit_codes = [
        int(row["exit_code"])
        for row in measured
        if row["unexpected_exit"] and row["exit_code"] is not None
    ]
    diagnostic_tail = next(
        (
            list(row["diagnostic_tail"])
            for row in reversed(measured)
            if row.get("diagnostic_tail")
        ),
        [],
    )
    continuity_ok = (
        startup_ok
        and not unexpected_exit
        and minimum_margin is not None
        and minimum_margin >= minimum_margin_seconds
        and maximum_progress_age <= maximum_progress_age_seconds
        and maximum_progress_gap <= maximum_progress_gap_seconds
        and maximum_silence <= maximum_silence_seconds
        and maximum_total_silence <= maximum_total_silence_seconds
        and startup_transport_errors == 0
        and max((int(row["transport_errors"]) for row in measured), default=0) == 0
    )
    return {
        "continuity_ok": continuity_ok,
        "startup_ok": startup_ok,
        "startup_delay_seconds": startup_delay,
        "startup_progress_age_seconds": startup_age,
        "startup_transport_errors": startup_transport_errors,
        "minimum_playback_margin_seconds": minimum_margin,
        "maximum_progress_age_seconds": maximum_progress_age,
        "maximum_progress_gap_seconds": maximum_progress_gap,
        "maximum_silence_seconds": maximum_silence,
        "total_silence_seconds": maximum_total_silence,
        "unexpected_exit": unexpected_exit,
        "unexpected_exit_codes": sorted(set(exit_codes)),
        "diagnostic_tail": diagnostic_tail,
        "transport_errors": max((int(row["transport_errors"]) for row in measured), default=0),
    }


def _write_json_line(handle: IO[str], payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def run(args: argparse.Namespace) -> int:
    ffmpeg = Path(args.ffmpeg).expanduser().resolve()
    if not ffmpeg.is_file():
        raise RuntimeError(f"FFmpeg is missing: {ffmpeg}")
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    streams = args.stream or list(DEFAULT_STREAMS)
    labels = [label for label, _url in streams]
    if len(labels) != len(set(labels)):
        raise RuntimeError("stream labels must be unique")

    states: list[StreamState] = []
    history: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
    started = time.monotonic()
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        _write_json_line(
            handle,
            {
                "type": "continuity_start",
                "timestamp": _utc_now(),
                "duration_seconds": args.duration_seconds,
                "sample_seconds": args.sample_seconds,
                "warmup_seconds": args.warmup_seconds,
                "readiness_stable_seconds": args.readiness_stable_seconds,
                "maximum_startup_delay_seconds": args.maximum_startup_delay_seconds,
                "labels": labels,
            },
        )
        try:
            states = [_start_stream(ffmpeg, label, url) for label, url in streams]
            warmup_deadline = started + args.warmup_seconds
            startup_deadline = started + args.maximum_startup_delay_seconds
            readiness_last_progress: dict[str, float | None] = {
                state.label: None for state in states
            }
            readiness_stable_since: dict[str, float | None] = {
                state.label: None for state in states
            }
            while True:
                now = time.monotonic()
                all_ready = True
                for state in states:
                    with state.lock:
                        last_progress = state.last_progress_monotonic
                    previous_progress = readiness_last_progress[state.label]
                    stable_since = readiness_stable_since[state.label]
                    if last_progress is None:
                        stable_since = None
                    elif last_progress != previous_progress:
                        if (
                            previous_progress is None
                            or last_progress - previous_progress
                            > args.maximum_progress_gap_seconds
                        ):
                            stable_since = last_progress
                        elif stable_since is None:
                            stable_since = last_progress
                        readiness_last_progress[state.label] = last_progress
                    if (
                        last_progress is None
                        or now - last_progress > args.maximum_progress_gap_seconds
                    ):
                        stable_since = None
                    readiness_stable_since[state.label] = stable_since
                    if (
                        stable_since is None
                        or now - stable_since < args.readiness_stable_seconds
                    ):
                        all_ready = False
                if now >= warmup_deadline and all_ready:
                    break
                if now >= max(warmup_deadline, startup_deadline):
                    break
                time.sleep(0.2)

            measurement_started = time.monotonic()
            readiness_satisfied = all_ready
            startup_rows = [
                _begin_measurement(
                    state,
                    measurement_started,
                    readiness_satisfied=readiness_satisfied,
                )
                for state in states
            ]
            _write_json_line(
                handle,
                {
                    "type": "continuity_measurement_start",
                    "timestamp": _utc_now(),
                    "warmup_elapsed_seconds": round(measurement_started - started, 3),
                    "readiness_stable_seconds": args.readiness_stable_seconds,
                    "all_mounts_stable": readiness_satisfied,
                    "streams": startup_rows,
                },
            )
            next_sample = measurement_started
            while time.monotonic() - measurement_started < args.duration_seconds:
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(min(0.25, next_sample - now))
                    continue
                rows = [_snapshot(state, now) for state in states]
                for row in rows:
                    history[str(row["label"])].append(row)
                _write_json_line(
                    handle,
                    {"type": "continuity_sample", "timestamp": _utc_now(), "streams": rows},
                )
                next_sample += args.sample_seconds
        finally:
            for state in states:
                if state.process.poll() is None:
                    state.process.terminate()
            for state in states:
                try:
                    state.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    state.process.kill()
                    state.process.wait(timeout=5)

        summary_streams: dict[str, dict[str, Any]] = {}
        for label in labels:
            summary_streams[label] = _evaluate(
                history[label],
                minimum_margin_seconds=args.minimum_margin_seconds,
                maximum_progress_age_seconds=args.maximum_progress_age_seconds,
                maximum_silence_seconds=args.maximum_silence_seconds,
                maximum_progress_gap_seconds=args.maximum_progress_gap_seconds,
                maximum_total_silence_seconds=args.maximum_total_silence_seconds,
                maximum_startup_delay_seconds=args.maximum_startup_delay_seconds,
            )
        summary = {
            "type": "continuity_summary",
            "timestamp": _utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "warmup_elapsed_seconds": round(measurement_started - started, 3),
            "measured_duration_seconds": round(time.monotonic() - measurement_started, 3),
            "warmup_seconds": args.warmup_seconds,
            "readiness_stable_seconds": args.readiness_stable_seconds,
            "continuity_ok": all(row["continuity_ok"] for row in summary_streams.values()),
            "streams": summary_streams,
        }
        _write_json_line(handle, summary)
    summary_path = output.with_suffix(output.suffix + ".summary.json")
    temporary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, summary_path)
    print(str(summary_path))
    return 0 if summary["continuity_ok"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Continuously decode multiple RadioTEDU streams and measure listener playback deficit"
    )
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--stream", action="append", type=_parse_stream_arg)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sample-seconds", type=float, default=2.0)
    parser.add_argument("--warmup-seconds", type=float, default=10.0)
    parser.add_argument("--readiness-stable-seconds", type=float, default=10.0)
    parser.add_argument("--maximum-startup-delay-seconds", type=float, default=60.0)
    parser.add_argument("--minimum-margin-seconds", type=float, default=-5.0)
    parser.add_argument("--maximum-progress-age-seconds", type=float, default=5.0)
    parser.add_argument("--maximum-progress-gap-seconds", type=float, default=2.0)
    parser.add_argument("--maximum-silence-seconds", type=float, default=0.25)
    parser.add_argument("--maximum-total-silence-seconds", type=float, default=0.5)
    parser.add_argument(
        "--output",
        default=str(
            Path(r"C:\ProgramData\RadioTEDU\OnAir\Recovery\continuity")
            / f"continuity-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        ),
    )
    args = parser.parse_args(argv)
    if not 10.0 <= args.duration_seconds <= 7 * 24 * 3600:
        parser.error("duration-seconds must be between 10 seconds and 7 days")
    if not 0.5 <= args.sample_seconds <= 60.0:
        parser.error("sample-seconds must be between 0.5 and 60")
    if not 0.0 <= args.warmup_seconds <= 300.0:
        parser.error("warmup-seconds must be between 0 and 300")
    if not 0.0 <= args.readiness_stable_seconds <= 300.0:
        parser.error("readiness-stable-seconds must be between 0 and 300")
    if not 1.0 <= args.maximum_startup_delay_seconds <= 600.0:
        parser.error("maximum-startup-delay-seconds must be between 1 and 600")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
