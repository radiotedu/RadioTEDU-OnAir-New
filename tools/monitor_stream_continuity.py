from __future__ import annotations

import argparse
import hashlib
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
# This is the checked-in code roster, not a live server inventory. Update the
# version and pin whenever DEFAULT_STREAMS changes; strict evidence fails closed
# if the roster and its pin diverge.
CANONICAL_STREAM_ROSTER_VERSION = 1
CANONICAL_STREAM_ROSTER_SHA256 = (
    "a5dced932450a080d28e8e2acf0338744312566e930de3bfd56590630edbb7b0"
)
_CLOCK = re.compile(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$")
_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(
    r"silence_end:\s*([0-9.]+).*?silence_duration:\s*([0-9.]+)"
)
_TRANSPORT_ERROR = re.compile(
    r"connection reset|connection refused|timed out|server returned|"
    r"input/output error|end of file|http error|broken pipe|error while decoding|"
    r"error submitting packet|invalid data found when processing input|"
    r"channel element.*not allocated|number of bands.*exceeds limit",
    re.IGNORECASE,
)
_PCM_SAMPLE_RATE = 48_000
_PCM_CHANNELS = 2
_PCM_BYTES_PER_SAMPLE = 2
_PCM_BYTES_PER_SECOND = _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_BYTES_PER_SAMPLE


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


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _endpoint_identity(label: str, url: str) -> dict[str, str | bool]:
    parsed = urlsplit(url)
    safe_url = parsed._replace(query="", fragment="").geturl() if parsed.query else url
    return {
        "label": label,
        "url": safe_url,
        "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "query_redacted": bool(parsed.query),
    }


def _canonical_stream_roster() -> tuple[dict[str, str], str]:
    rows = sorted(DEFAULT_STREAMS)
    roster = dict(rows)
    if len(roster) != len(rows) or len(set(roster.values())) != len(rows):
        raise RuntimeError("checked-in canonical stream roster contains duplicates")
    canonical_bytes = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    if digest != CANONICAL_STREAM_ROSTER_SHA256:
        raise RuntimeError(
            "checked-in canonical stream roster changed without updating its "
            "version and SHA-256 pin"
        )
    return roster, digest


def _roster_difference(expected: dict[str, str], actual: dict[str, str]) -> str:
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    changed = sorted(
        label
        for label in set(expected) & set(actual)
        if expected[label] != actual[label]
    )
    details = []
    if missing:
        details.append(f"missing labels: {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected labels: {', '.join(unexpected)}")
    if changed:
        details.append(f"endpoint mismatch: {', '.join(changed)}")
    return "; ".join(details) or "roster mapping differs"


def _load_expected_roster(
    path: Path, streams: list[tuple[str, str]]
) -> dict[str, Any]:
    """Validate a manifest against the pinned checked-in code roster.

    A caller-supplied source string is provenance only; it cannot establish the
    current runtime's authoritative public-output inventory.
    """
    manifest_bytes = path.expanduser().resolve().read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("expected-roster manifest must be valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("expected-roster manifest must use schema_version 1")
    source = manifest.get("source")
    captured_at = manifest.get("captured_at_utc")
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError("expected-roster manifest must identify its claimed source")
    if not isinstance(captured_at, str):
        raise RuntimeError("expected-roster manifest must include captured_at_utc")
    try:
        captured_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("captured_at_utc must be an ISO-8601 timestamp") from exc
    if captured_time.tzinfo is None:
        raise RuntimeError("captured_at_utc must include a timezone")
    roster_age = (datetime.now(UTC) - captured_time.astimezone(UTC)).total_seconds()
    if roster_age < -60.0 or roster_age > 15 * 60.0:
        raise RuntimeError("expected-roster manifest must be no more than 15 minutes old")
    rows = manifest.get("streams")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("expected-roster manifest must include a non-empty streams list")

    expected: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("each expected-roster stream must be an object")
        label, url = row.get("label"), row.get("url")
        if not isinstance(label, str) or not isinstance(url, str):
            raise RuntimeError("each expected-roster stream needs a label and URL")
        parsed_label, parsed_url = _parse_stream_arg(f"{label}={url}")
        if urlsplit(parsed_url).query:
            raise RuntimeError(
                "strict public-listener roster URLs must not contain query credentials"
            )
        if parsed_label in expected:
            raise RuntimeError(f"expected-roster manifest repeats label {parsed_label!r}")
        if parsed_url in expected.values():
            raise RuntimeError("expected-roster manifest repeats a public endpoint")
        expected[parsed_label] = parsed_url

    canonical, canonical_digest = _canonical_stream_roster()
    if expected != canonical:
        raise RuntimeError(
            "expected-roster manifest does not match checked-in canonical code "
            f"roster v{CANONICAL_STREAM_ROSTER_VERSION} sha256={canonical_digest} "
            "(this roster is not a live runtime authority): "
            + _roster_difference(canonical, expected)
        )

    supplied = dict(streams)
    if len(supplied) != len(streams):
        raise RuntimeError("stream labels must be unique")
    if supplied != canonical:
        raise RuntimeError(
            "provided streams do not match checked-in canonical code roster "
            f"v{CANONICAL_STREAM_ROSTER_VERSION} sha256={canonical_digest} "
            "(this roster is not a live runtime authority): "
            + _roster_difference(canonical, supplied)
        )
    return {
        "path": str(path.expanduser().resolve()),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_source_claim": source.strip(),
        "captured_at_utc": (
            captured_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
        ),
        "age_at_start_seconds": round(max(0.0, roster_age), 3),
        "stream_count": len(expected),
        "canonical_roster_version": CANONICAL_STREAM_ROSTER_VERSION,
        "canonical_roster_sha256": canonical_digest,
        "canonical_code_roster_match": True,
        "live_runtime_roster_authoritative": False,
    }


def build_ffmpeg_command(
    ffmpeg: Path, url: str, *, decoded_pcm: bool = False
) -> list[str]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info" if decoded_pcm else "warning",
        "-nostats",
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
    ]
    if decoded_pcm:
        # Count decoded samples rather than inferring delivery from occasional
        # FFmpeg progress messages or from signal level. Silence can be valid
        # program material; PCM bytes continue to arrive during valid silence.
        command.extend(
            [
                "-map",
                "0:a:0",
                "-ac",
                str(_PCM_CHANNELS),
                "-ar",
                str(_PCM_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                # Keep signal diagnostics separate from sample delivery.
                # Quiet programme audio is valid and does not fail the
                # decoded-delivery gate, but operators still need measured
                # silence to correlate with source-generated filler frames.
                "-af",
                "silencedetect=noise=-65dB:d=0.5",
                "-flush_packets",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ]
        )
    else:
        command.extend(
            [
                "-af",
                # Keep the legacy silence diagnostics for existing callers.
                "silencedetect=noise=-75dB:d=0.01",
                "-f",
                "null",
                "NUL" if os.name == "nt" else "/dev/null",
                "-progress",
                "pipe:1",
            ]
        )
    return command


@dataclass
class StreamState:
    label: str
    url: str
    process: subprocess.Popen[Any]
    started_monotonic: float
    decoded_audio_mode: bool = False
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
    measurement_minimum_playback_margin: float | None = None
    decoded_audio_bytes_total: int = 0
    measurement_audio_bytes_baseline: int = 0
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
    input_audio_description: str | None = None
    reader_restart_count: int = 0
    reader_restart_due_monotonic: float | None = None
    reader_previous_decoded_audio_bytes: int = 0
    reader_threads: list[threading.Thread] = field(default_factory=list)


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


def _read_decoded_audio(state: StreamState, stream: IO[bytes]) -> None:
    """Measure decoded PCM delivery without treating quiet audio as a dropout."""
    read_chunk = getattr(stream, "read1", stream.read)
    while True:
        chunk = read_chunk(65_536)
        if not chunk:
            return
        now = time.monotonic()
        with state.lock:
            previous = state.last_progress_monotonic
            if state.measurement_started_monotonic is not None:
                # Check the reserve before newly arrived PCM repairs a deficit.
                # A transient underrun between periodic samples must stay visible.
                margin = state.media_seconds - (
                    state.measurement_media_baseline
                    + now - state.measurement_started_monotonic
                )
                current = state.measurement_minimum_playback_margin
                state.measurement_minimum_playback_margin = (
                    margin if current is None else min(current, margin)
                )
            if state.first_progress_monotonic is None:
                state.first_progress_monotonic = now
                state.first_media_seconds = 0.0
            if previous is not None:
                gap = now - previous
                state.max_progress_gap_seconds = max(
                    state.max_progress_gap_seconds, gap
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
            elif state.measurement_started_monotonic is not None:
                state.measurement_max_progress_gap_seconds = max(
                    state.measurement_max_progress_gap_seconds,
                    now - state.measurement_started_monotonic,
                )
            state.decoded_audio_bytes_total += len(chunk)
            state.media_seconds = (
                state.decoded_audio_bytes_total / _PCM_BYTES_PER_SECOND
            )
            state.last_progress_monotonic = now
            if state.measurement_started_monotonic is not None:
                state.measurement_last_progress_monotonic = now


def _read_diagnostics(state: StreamState, stream: IO[str] | IO[bytes]) -> None:
    while True:
        raw_line = stream.readline()
        if not raw_line:
            return
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace").strip()
        else:
            line = raw_line.strip()
        if not line:
            continue
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        with state.lock:
            if state.input_audio_description is None and re.search(r"Stream #0:\d+.*Audio:", line):
                state.input_audio_description = line.partition("Audio:")[2].strip()
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
        decoded_audio_bytes = max(
            0, state.decoded_audio_bytes_total - state.measurement_audio_bytes_baseline
        )
        return {
            "label": state.label,
            "process_alive": state.process.poll() is None,
            "exit_code": state.process.poll(),
            "last_unexpected_exit_code": state.unexpected_exit_code,
            "reader_restart_count": state.reader_restart_count,
            "decoded_audio_bytes_scope": "current_reader_generation",
            "reader_previous_decoded_audio_bytes": state.reader_previous_decoded_audio_bytes,
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
            "decoded_audio_mode": state.decoded_audio_mode,
            "input_audio_description": state.input_audio_description,
            "decoded_audio_bytes": decoded_audio_bytes if state.decoded_audio_mode else None,
            "decoded_audio_seconds": (
                round(decoded_audio_bytes / _PCM_BYTES_PER_SECOND, 6)
                if state.decoded_audio_mode
                else None
            ),
            "decoded_audio_gap_seconds": (
                round(maximum_progress_gap, 3)
                if state.decoded_audio_mode
                else None
            ),
            "playback_margin_seconds": (
                round(playback_margin, 3) if playback_margin is not None else None
            ),
            "minimum_playback_margin_seconds": state.measurement_minimum_playback_margin,
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
            "unexpected_exit": bool(
                state.unexpected_exit_code is not None
                or (state.process.poll() is not None and not stopping)
            ),
        }


def _start_stream(
    ffmpeg: Path, label: str, url: str, *, decoded_pcm: bool = False
) -> StreamState:
    process = subprocess.Popen(
        build_ffmpeg_command(ffmpeg, url, decoded_pcm=decoded_pcm),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not decoded_pcm,
        encoding=None if decoded_pcm else "utf-8",
        errors=None if decoded_pcm else "replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None and process.stderr is not None
    state = StreamState(
        label, url, process, time.monotonic(), decoded_audio_mode=decoded_pcm
    )
    audio_thread = threading.Thread(
        target=_read_decoded_audio if decoded_pcm else _read_progress,
        args=(state, process.stdout),
        name=(
            f"continuity-audio-{label}"
            if decoded_pcm
            else f"continuity-progress-{label}"
        ),
        daemon=True,
    )
    diagnostic_thread = threading.Thread(
        target=_read_diagnostics,
        args=(state, process.stderr),
        name=f"continuity-diagnostics-{label}",
        daemon=True,
    )
    state.reader_threads.extend((audio_thread, diagnostic_thread))
    audio_thread.start()
    diagnostic_thread.start()
    return state


def _begin_measurement(
    state: StreamState,
    now: float,
    *,
    readiness_satisfied: bool = True,
    listener_buffer_seconds: float = 0.0,
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
        reserve = min(max(0.0, float(listener_buffer_seconds)), state.media_seconds)
        state.measurement_media_baseline = state.media_seconds - reserve
        state.measurement_minimum_playback_margin = reserve
        state.measurement_audio_bytes_baseline = state.decoded_audio_bytes_total
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
    decoded_audio_mode: bool = False,
    maximum_decoded_audio_gap_seconds: float = 0.25,
) -> dict[str, Any]:
    metrics = _empty_evaluation_metrics()
    for row in snapshots:
        _accumulate_evaluation_metrics(metrics, row)
    return _evaluate_metrics(
        metrics,
        minimum_margin_seconds=minimum_margin_seconds,
        maximum_progress_age_seconds=maximum_progress_age_seconds,
        maximum_silence_seconds=maximum_silence_seconds,
        maximum_progress_gap_seconds=maximum_progress_gap_seconds,
        maximum_total_silence_seconds=maximum_total_silence_seconds,
        maximum_startup_delay_seconds=maximum_startup_delay_seconds,
        decoded_audio_mode=decoded_audio_mode,
        maximum_decoded_audio_gap_seconds=maximum_decoded_audio_gap_seconds,
    )


def _empty_evaluation_metrics() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "input_audio_description": None,
        "has_startup_metrics": False,
        "startup_delay": None,
        "startup_age": 0.0,
        "startup_transport_errors": 0,
        "readiness_ok": True,
        "minimum_margin": None,
        "maximum_progress_age": 0.0,
        "maximum_progress_gap": 0.0,
        "maximum_decoded_audio_gap": 0.0,
        "maximum_decoded_audio_seconds": 0.0,
        "maximum_silence": 0.0,
        "maximum_total_silence": 0.0,
        "unexpected_exit": False,
        "exit_codes": set(),
        "diagnostic_tail": [],
        "transport_errors": 0,
    }


def _accumulate_evaluation_metrics(metrics: dict[str, Any], row: dict[str, Any]) -> None:
    if not row.get("measurement_active", True):
        return
    metrics["sample_count"] += 1
    if row.get("input_audio_description"):
        metrics["input_audio_description"] = row["input_audio_description"]
    if "startup_delay_seconds" in row:
        metrics["has_startup_metrics"] = True
        startup_delay = row["startup_delay_seconds"]
        if startup_delay is not None:
            value = float(startup_delay)
            metrics["startup_delay"] = max(
                metrics["startup_delay"] or value, value
            )
        metrics["startup_age"] = max(
            metrics["startup_age"],
            float(row.get("startup_progress_age_seconds") or 0.0),
        )
        metrics["startup_transport_errors"] = max(
            metrics["startup_transport_errors"],
            int(row.get("startup_transport_errors", 0)),
        )
        metrics["readiness_ok"] = metrics["readiness_ok"] and bool(
            row.get("readiness_satisfied", False)
        )
    margin = row.get("playback_margin_seconds")
    observed_minimum = row.get("minimum_playback_margin_seconds")
    if observed_minimum is not None:
        margin = min(float(margin), float(observed_minimum)) if margin is not None else observed_minimum
    if margin is not None:
        value = float(margin)
        current = metrics["minimum_margin"]
        metrics["minimum_margin"] = value if current is None else min(current, value)
    metrics["maximum_progress_age"] = max(
        metrics["maximum_progress_age"], float(row["progress_age_seconds"])
    )
    metrics["maximum_progress_gap"] = max(
        metrics["maximum_progress_gap"],
        float(row.get("max_progress_gap_seconds", 0.0)),
    )
    if row.get("decoded_audio_gap_seconds") is not None:
        metrics["maximum_decoded_audio_gap"] = max(
            metrics["maximum_decoded_audio_gap"],
            float(row["decoded_audio_gap_seconds"]),
        )
    if row.get("decoded_audio_seconds") is not None:
        metrics["maximum_decoded_audio_seconds"] = max(
            metrics["maximum_decoded_audio_seconds"],
            float(row["decoded_audio_seconds"]),
        )
    metrics["maximum_silence"] = max(
        metrics["maximum_silence"], float(row.get("max_silence_seconds", 0.0))
    )
    metrics["maximum_total_silence"] = max(
        metrics["maximum_total_silence"],
        float(row.get("total_silence_seconds", 0.0)),
    )
    if bool(row.get("unexpected_exit", False)):
        metrics["unexpected_exit"] = True
        if row.get("exit_code") is not None:
            metrics["exit_codes"].add(int(row["exit_code"]))
        if row.get("last_unexpected_exit_code") is not None:
            metrics["exit_codes"].add(int(row["last_unexpected_exit_code"]))
    metrics["transport_errors"] = max(
        metrics["transport_errors"], int(row.get("transport_errors", 0))
    )
    if row.get("diagnostic_tail"):
        metrics["diagnostic_tail"] = list(row["diagnostic_tail"])


def _evaluate_metrics(
    metrics: dict[str, Any],
    *,
    minimum_margin_seconds: float,
    maximum_progress_age_seconds: float,
    maximum_silence_seconds: float,
    maximum_progress_gap_seconds: float,
    maximum_total_silence_seconds: float,
    maximum_startup_delay_seconds: float,
    decoded_audio_mode: bool = False,
    maximum_decoded_audio_gap_seconds: float = 0.25,
) -> dict[str, Any]:
    has_startup_metrics = bool(metrics["has_startup_metrics"])
    startup_delay = metrics["startup_delay"]
    startup_age = float(metrics["startup_age"])
    startup_transport_errors = int(metrics["startup_transport_errors"])
    startup_ok = not has_startup_metrics or (
        startup_delay is not None
        and startup_delay <= maximum_startup_delay_seconds
        and startup_age <= maximum_progress_age_seconds
        and startup_transport_errors == 0
        and bool(metrics["readiness_ok"])
    )
    minimum_margin = metrics["minimum_margin"]
    maximum_progress_age = float(metrics["maximum_progress_age"])
    maximum_progress_gap = float(metrics["maximum_progress_gap"])
    maximum_decoded_audio_gap = float(metrics["maximum_decoded_audio_gap"])
    maximum_decoded_audio_seconds = float(metrics["maximum_decoded_audio_seconds"])
    maximum_silence = float(metrics["maximum_silence"])
    maximum_total_silence = float(metrics["maximum_total_silence"])
    unexpected_exit = bool(metrics["unexpected_exit"])
    exit_codes = metrics["exit_codes"]
    diagnostic_tail = list(metrics["diagnostic_tail"])
    transport_errors = int(metrics["transport_errors"])
    if decoded_audio_mode:
        gap_ok = (
            maximum_decoded_audio_gap <= maximum_decoded_audio_gap_seconds
            and maximum_decoded_audio_seconds > 0.0
        )
    else:
        gap_ok = maximum_progress_gap <= maximum_progress_gap_seconds
    continuity_ok = (
        metrics["sample_count"] > 0
        and startup_ok
        and not unexpected_exit
        and minimum_margin is not None
        and minimum_margin >= minimum_margin_seconds
        and maximum_progress_age <= maximum_progress_age_seconds
        and gap_ok
        and (
            decoded_audio_mode
            or (
                maximum_silence <= maximum_silence_seconds
                and maximum_total_silence <= maximum_total_silence_seconds
            )
        )
        and startup_transport_errors == 0
        and transport_errors == 0
    )
    return {
        "continuity_ok": continuity_ok,
        "input_audio_description": metrics.get("input_audio_description"),
        "measurement_samples": metrics["sample_count"],
        "startup_ok": startup_ok,
        "startup_delay_seconds": startup_delay,
        "startup_progress_age_seconds": startup_age,
        "startup_transport_errors": startup_transport_errors,
        "minimum_playback_margin_seconds": minimum_margin,
        "maximum_progress_age_seconds": maximum_progress_age,
        "maximum_progress_gap_seconds": maximum_progress_gap,
        "maximum_decoded_audio_gap_seconds": maximum_decoded_audio_gap,
        "maximum_decoded_audio_seconds": maximum_decoded_audio_seconds,
        "maximum_silence_seconds": maximum_silence,
        "total_silence_seconds": maximum_total_silence,
        "unexpected_exit": unexpected_exit,
        "unexpected_exit_codes": sorted(exit_codes),
        "diagnostic_tail": diagnostic_tail,
        "transport_errors": transport_errors,
    }


def _write_json_line(handle: IO[str], payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _retry_exited_readers(
    states: list[StreamState],
    ffmpeg: Path,
    now: float,
    metrics: dict[str, dict[str, Any]],
    handle: IO[str],
    *,
    measuring: bool,
) -> None:
    """Resume observation after an exit while retaining the failed-run evidence.

    Samples after a retry describe the new reader generation. The global run
    duration and sticky failure metrics never restart or become a clean pass.
    Only diagnostic listener processes are replaced; sources are not touched.
    """
    for index, state in enumerate(states):
        exit_code = state.process.poll()
        if exit_code is None:
            continue
        if state.reader_restart_due_monotonic is None:
            state.unexpected_exit_code = int(exit_code)
            metric = metrics[state.label]
            metric["unexpected_exit"] = True
            metric["exit_codes"].add(int(exit_code))
            _accumulate_evaluation_metrics(metric, _snapshot(state, now))
            delay = min(60.0, 5.0 * 2 ** min(state.reader_restart_count, 4))
            state.reader_restart_due_monotonic = now + delay
            _write_json_line(handle, {
                "type": "continuity_reader_exit", "timestamp": _utc_now(),
                "label": state.label, "exit_code": exit_code,
                "reader_restart_count": state.reader_restart_count,
                "retry_delay_seconds": delay,
                "clean_run_possible": False,
            })
            continue
        if now < state.reader_restart_due_monotonic:
            continue
        for thread in state.reader_threads:
            thread.join(timeout=0.1)
        _accumulate_evaluation_metrics(metrics[state.label], _snapshot(state, now))
        try:
            replacement = _start_stream(
                ffmpeg, state.label, state.url, decoded_pcm=state.decoded_audio_mode
            )
        except OSError as exc:
            state.reader_restart_due_monotonic = now + 60.0
            _write_json_line(handle, {
                "type": "continuity_reader_retry_failed", "timestamp": _utc_now(),
                "label": state.label, "error_type": type(exc).__name__,
                "clean_run_possible": False,
            })
            continue
        with state.lock, replacement.lock:
            replacement.reader_restart_count = state.reader_restart_count + 1
            replacement.unexpected_exit_code = state.unexpected_exit_code
            replacement.reader_previous_decoded_audio_bytes = (
                state.reader_previous_decoded_audio_bytes + state.decoded_audio_bytes_total
            )
            replacement.transport_errors += state.transport_errors
        if measuring:
            _begin_measurement(replacement, now, readiness_satisfied=False)
        states[index] = replacement
        if not any(thread.is_alive() for thread in state.reader_threads):
            for stream_name in ("stdout", "stderr"):
                stream = getattr(state.process, stream_name, None)
                if stream is not None:
                    stream.close()
        _write_json_line(handle, {
            "type": "continuity_reader_restarted", "timestamp": _utc_now(),
            "label": state.label, "reader_restart_count": replacement.reader_restart_count,
            "reader_previous_decoded_audio_bytes": replacement.reader_previous_decoded_audio_bytes,
            "clean_run_possible": False,
        })


def run(args: argparse.Namespace) -> int:
    ffmpeg = Path(args.ffmpeg).expanduser().resolve()
    if not ffmpeg.is_file():
        raise RuntimeError(f"FFmpeg is missing: {ffmpeg}")
    output = Path(args.output).expanduser().resolve()
    streams = args.stream or list(DEFAULT_STREAMS)
    labels = [label for label, _url in streams]
    if len(labels) != len(set(labels)):
        raise RuntimeError("stream labels must be unique")
    roster = (
        _load_expected_roster(Path(args.expected_roster_file), streams)
        if args.expected_roster_file
        else None
    )
    decoded_pcm = roster is not None
    if decoded_pcm and (
        output.exists()
        or output.with_suffix(output.suffix + ".summary.json").exists()
    ):
        raise RuntimeError("strict evidence output already exists; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_path = Path(__file__).resolve()
    identity = {
        "monitor_source_path": str(source_path),
        "monitor_source_sha256": _sha256_path(source_path),
        "ffmpeg_path": str(ffmpeg),
        "ffmpeg_sha256": _sha256_path(ffmpeg),
        "roster_manifest": roster,
        "canonical_code_roster_match": bool(
            roster and roster.get("canonical_code_roster_match") is True
        ),
        "canonical_roster_version": (
            roster.get("canonical_roster_version") if roster else None
        ),
        "canonical_roster_sha256": (
            roster.get("canonical_roster_sha256") if roster else None
        ),
        "live_runtime_roster_authoritative": False,
        "roster_authority_scope": "checked-in DEFAULT_STREAMS only; live runtime inventory not verified",
        "endpoints": [
            _endpoint_identity(label, url)
            for label, url in streams
        ],
    }
    settings = {
        "duration_seconds": args.duration_seconds,
        "sample_seconds": args.sample_seconds,
        "warmup_seconds": args.warmup_seconds,
        "readiness_stable_seconds": args.readiness_stable_seconds,
        "maximum_startup_delay_seconds": args.maximum_startup_delay_seconds,
        "minimum_margin_seconds": args.minimum_margin_seconds,
        "maximum_progress_age_seconds": args.maximum_progress_age_seconds,
        "maximum_progress_gap_seconds": args.maximum_progress_gap_seconds,
        "maximum_decoded_audio_gap_seconds": args.maximum_decoded_audio_gap_seconds,
        "maximum_silence_seconds": args.maximum_silence_seconds,
        "maximum_total_silence_seconds": args.maximum_total_silence_seconds,
        "silence_thresholds_applied": not decoded_pcm,
        "legacy_progress_gap_threshold_applied": not decoded_pcm,
        "decoded_audio_mode": decoded_pcm,
        "decoded_audio_format": (
            f"s16le/{_PCM_SAMPLE_RATE}Hz/{_PCM_CHANNELS}ch" if decoded_pcm else None
        ),
        "decoded_audio_stream_selection": "0:a:0" if decoded_pcm else None,
        "listener_buffer_seconds": getattr(args, "listener_buffer_seconds", 0.0),
        "restart_exited_readers": getattr(args, "restart_exited_readers", False),
        "reader_retry_clears_failures": False,
        "silence_diagnostics_applied": True,
        "silence_diagnostic_threshold_db": -65 if decoded_pcm else None,
        "ffmpeg_input_policy": {
            "rw_timeout_microseconds": 15_000_000,
            "reconnect": True,
            "reconnect_at_eof": True,
            "reconnect_streamed": True,
            "reconnect_delay_max_seconds": 5,
            "threads": 1,
        },
        "output_path": str(output),
    }

    states: list[StreamState] = []
    metrics = {label: _empty_evaluation_metrics() for label in labels}
    started = time.monotonic()
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        _write_json_line(
            handle,
            {
                "type": "continuity_start",
                "timestamp": _utc_now(),
                "mode": (
                    "decoded_pcm_checked_in_canonical_roster_only"
                    if decoded_pcm
                    else "legacy_progress_roster_unverified"
                ),
                **identity,
                "config": settings,
                "labels": labels,
            },
        )
        try:
            for label, url in streams:
                states.append(_start_stream(ffmpeg, label, url, decoded_pcm=decoded_pcm))
            warmup_deadline = started + args.warmup_seconds
            startup_deadline = started + args.maximum_startup_delay_seconds
            readiness_gap_limit = (
                args.maximum_decoded_audio_gap_seconds
                if decoded_pcm
                else args.maximum_progress_gap_seconds
            )
            readiness_last_progress: dict[str, float | None] = {
                state.label: None for state in states
            }
            readiness_stable_since: dict[str, float | None] = {
                state.label: None for state in states
            }
            while True:
                now = time.monotonic()
                if getattr(args, "restart_exited_readers", False):
                    _retry_exited_readers(states, ffmpeg, now, metrics, handle, measuring=False)
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
                            > readiness_gap_limit
                        ):
                            stable_since = last_progress
                        elif stable_since is None:
                            stable_since = last_progress
                        readiness_last_progress[state.label] = last_progress
                    if (
                        last_progress is None
                        or now - last_progress > readiness_gap_limit
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
                    listener_buffer_seconds=getattr(args, "listener_buffer_seconds", 0.0),
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
                    "canonical_code_roster_match": bool(
                        roster and roster.get("canonical_code_roster_match") is True
                    ),
                    "live_runtime_roster_authoritative": False,
                    "streams": startup_rows,
                },
            )
            next_sample = measurement_started
            measurement_deadline = measurement_started + args.duration_seconds
            measurement_boundary_monotonic: float | None = None
            while True:
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(min(0.25, next_sample - now))
                    continue
                at_boundary = now >= measurement_deadline
                if not at_boundary and getattr(args, "restart_exited_readers", False):
                    _retry_exited_readers(states, ffmpeg, now, metrics, handle, measuring=True)
                rows = [_snapshot(state, now) for state in states]
                for row in rows:
                    _accumulate_evaluation_metrics(metrics[str(row["label"])], row)
                _write_json_line(
                    handle,
                    {
                        "type": "continuity_sample",
                        "timestamp": _utc_now(),
                        "measurement_elapsed_seconds": round(now - measurement_started, 3),
                        "duration_boundary_sample": at_boundary,
                        "streams": rows,
                    },
                )
                if at_boundary:
                    measurement_boundary_monotonic = now
                    break
                next_sample = min(next_sample + args.sample_seconds, measurement_deadline)
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
            summary_streams[label] = _evaluate_metrics(
                metrics[label],
                minimum_margin_seconds=args.minimum_margin_seconds,
                maximum_progress_age_seconds=args.maximum_progress_age_seconds,
                maximum_silence_seconds=args.maximum_silence_seconds,
                maximum_progress_gap_seconds=args.maximum_progress_gap_seconds,
                maximum_total_silence_seconds=args.maximum_total_silence_seconds,
                maximum_startup_delay_seconds=args.maximum_startup_delay_seconds,
                decoded_audio_mode=decoded_pcm,
                maximum_decoded_audio_gap_seconds=args.maximum_decoded_audio_gap_seconds,
            )
        finished_monotonic = time.monotonic()
        measured_until = measurement_boundary_monotonic or finished_monotonic
        measured_duration = measured_until - measurement_started
        all_streams_ok = all(row["continuity_ok"] for row in summary_streams.values())
        continuity_ok = all_streams_ok and (
            not decoded_pcm
            or (
                roster is not None
                and roster.get("canonical_code_roster_match") is True
                and measurement_boundary_monotonic is not None
                and measured_duration >= args.duration_seconds
            )
        )
        summary = {
            "type": "continuity_summary",
            "timestamp": _utc_now(),
            "mode": (
                "decoded_pcm_checked_in_canonical_roster_only"
                if decoded_pcm
                else "legacy_progress_roster_unverified"
            ),
            **identity,
            "config": settings,
            "elapsed_seconds": round(finished_monotonic - started, 3),
            "warmup_elapsed_seconds": round(measurement_started - started, 3),
            "measured_duration_seconds": round(measured_duration, 3),
            "requested_measurement_duration_seconds": args.duration_seconds,
            "duration_boundary_sampled": measurement_boundary_monotonic is not None,
            "duration_boundary_lateness_seconds": (
                round(measurement_boundary_monotonic - measurement_deadline, 3)
                if measurement_boundary_monotonic is not None
                else None
            ),
            "warmup_seconds": args.warmup_seconds,
            "readiness_stable_seconds": args.readiness_stable_seconds,
            "continuity_ok": continuity_ok,
            "evidence_grade": (
                "decoded_pcm_checked_in_canonical_roster_not_live_runtime_authority"
                if decoded_pcm
                else "legacy_progress_roster_unverified"
            ),
            "streams": summary_streams,
        }
        _write_json_line(handle, summary)
    summary_path = output.with_suffix(output.suffix + ".summary.json")
    summary["jsonl_sha256"] = _sha256_path(output)
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
    parser.add_argument(
        "--expected-roster-file",
        help=(
            "fresh schema-v1 JSON with source, captured_at_utc, and streams "
            "[{label,url}] exactly matching the pinned checked-in DEFAULT_STREAMS; "
            "enables decoded-PCM evidence for that code roster only, not the live runtime inventory"
        ),
    )
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sample-seconds", type=float, default=2.0)
    parser.add_argument("--warmup-seconds", type=float, default=10.0)
    parser.add_argument("--readiness-stable-seconds", type=float, default=10.0)
    parser.add_argument("--maximum-startup-delay-seconds", type=float, default=60.0)
    parser.add_argument("--minimum-margin-seconds", type=float, default=-5.0)
    parser.add_argument("--listener-buffer-seconds", type=float, default=0.0)
    parser.add_argument(
        "--restart-exited-readers", action="store_true",
        help="Retry exited diagnostic listeners; retain all failures and never reset the run clock.",
    )
    parser.add_argument("--maximum-progress-age-seconds", type=float, default=5.0)
    parser.add_argument("--maximum-progress-gap-seconds", type=float, default=2.0)
    parser.add_argument("--maximum-decoded-audio-gap-seconds", type=float, default=0.25)
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
    if not 0.0 <= args.maximum_decoded_audio_gap_seconds <= 60.0:
        parser.error("maximum-decoded-audio-gap-seconds must be between 0 and 60")
    if not 0.0 <= args.listener_buffer_seconds <= 10.0:
        parser.error("listener-buffer-seconds must be between 0 and 10")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
