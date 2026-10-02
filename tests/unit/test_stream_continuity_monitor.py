from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.monitor_stream_continuity import (
    CANONICAL_STREAM_ROSTER_SHA256,
    CANONICAL_STREAM_ROSTER_VERSION,
    DEFAULT_STREAMS,
    _begin_measurement,
    _evaluate,
    _load_expected_roster,
    _parse_clock,
    _parse_stream_arg,
    _read_decoded_audio,
    _read_diagnostics,
    _retry_exited_readers,
    _snapshot,
    _empty_evaluation_metrics,
    StreamState,
    build_ffmpeg_command,
)


def test_parse_clock_uses_media_time() -> None:
    assert _parse_clock("01:02:03.500") == pytest.approx(3723.5)
    assert _parse_clock("invalid") is None


def test_stream_argument_rejects_embedded_credentials() -> None:
    assert _parse_stream_arg("lofi=http://example.test/lofi") == (
        "lofi",
        "http://example.test/lofi",
    )
    with pytest.raises(Exception):
        _parse_stream_arg("lofi=http://user:secret@example.test/lofi")


def test_ffmpeg_command_enables_reconnect_and_silence_detection() -> None:
    command = build_ffmpeg_command(Path("ffmpeg.exe"), "http://example.test/stream")
    assert "-reconnect_streamed" in command
    assert "-progress" in command
    assert command[command.index("-threads") + 1] == "1"
    detector = next(value for value in command if value.startswith("silencedetect="))
    assert "d=0.01" in detector


def test_strict_ffmpeg_command_emits_normalized_decoded_pcm() -> None:
    command = build_ffmpeg_command(
        Path("ffmpeg.exe"), "http://example.test/stream", decoded_pcm=True
    )
    assert "-reconnect_streamed" in command
    assert command[command.index("-map") + 1] == "0:a:0"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-f") + 1] == "s16le"
    assert "pipe:1" in command
    assert "-progress" not in command
    # A transparent diagnostic filter measures quiet passages independently
    # of sample delivery; it must not trim or suppress valid programme audio.
    assert command[command.index("-af") + 1] == "silencedetect=noise=-65dB:d=0.5"


def test_expected_roster_rejects_matching_but_incomplete_eight_mount_subset(
    tmp_path: Path,
) -> None:
    roster_path = tmp_path / "public-roster.json"
    subset = list(DEFAULT_STREAMS[:8])
    roster_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "source": "caller-claimed complete output list",
                "streams": [
                    {"label": label, "url": url} for label, url in subset
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        RuntimeError,
        match="checked-in canonical code roster.*missing labels:",
    ):
        _load_expected_roster(roster_path, subset)


def test_expected_roster_accepts_exact_fresh_mount_list(tmp_path: Path) -> None:
    roster_path = tmp_path / "public-roster.json"
    roster_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "source": "caller-claimed output list",
                "streams": [
                    {"label": label, "url": url}
                    for label, url in DEFAULT_STREAMS
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence = _load_expected_roster(
        roster_path,
        list(DEFAULT_STREAMS),
    )
    assert evidence["stream_count"] == len(DEFAULT_STREAMS)
    assert evidence["manifest_source_claim"] == "caller-claimed output list"
    assert len(evidence["sha256"]) == 64
    assert evidence["canonical_roster_version"] == CANONICAL_STREAM_ROSTER_VERSION
    assert evidence["canonical_roster_sha256"] == CANONICAL_STREAM_ROSTER_SHA256
    assert evidence["canonical_code_roster_match"] is True
    assert evidence["live_runtime_roster_authoritative"] is False


def test_decoded_audio_reader_counts_pcm_even_when_samples_are_silent() -> None:
    state = StreamState(
        label="test",
        url="https://example.test/stream",
        process=SimpleNamespace(),  # type: ignore[arg-type]
        started_monotonic=0.0,
        decoded_audio_mode=True,
    )
    _read_decoded_audio(state, io.BytesIO(bytes(1_920)))
    assert state.decoded_audio_bytes_total == 1_920
    assert state.media_seconds == pytest.approx(0.01)
    assert state.first_progress_monotonic is not None


def test_listener_buffer_covers_packet_delivery_but_preserves_transient_underrun(monkeypatch):
    from tools import monitor_stream_continuity as monitor

    def receive_after(delay):
        state = StreamState("test", "http://example.test/audio", SimpleNamespace(poll=lambda: None), 0.0, decoded_audio_mode=True)
        state.media_seconds = 10.0
        state.decoded_audio_bytes_total = 10 * 192000
        state.first_progress_monotonic = 0.0
        state.last_progress_monotonic = 10.0
        _begin_measurement(state, 10.0, listener_buffer_seconds=4.0)
        monkeypatch.setattr(monitor.time, "monotonic", lambda: 10.0 + delay)
        _read_decoded_audio(state, io.BytesIO(bytes(192000)))
        return monitor._snapshot(state, 10.0 + delay)

    buffered = receive_after(3.9)
    assert buffered["minimum_playback_margin_seconds"] == pytest.approx(0.1)
    recovered = receive_after(4.05)
    assert recovered["playback_margin_seconds"] > 0
    assert recovered["minimum_playback_margin_seconds"] == pytest.approx(-0.05)
    metrics = monitor._empty_evaluation_metrics()
    monitor._accumulate_evaluation_metrics(metrics, recovered)
    assert metrics["minimum_margin"] == pytest.approx(-0.05)


def test_diagnostics_accumulate_many_short_silence_events() -> None:
    state = StreamState(
        label="test",
        url="http://example.test/stream",
        process=SimpleNamespace(),  # type: ignore[arg-type]
        started_monotonic=0.0,
    )
    _read_diagnostics(
        state,
        io.StringIO(
            "[silencedetect] silence_start: 1.000\n"
            "[silencedetect] silence_end: 1.021 | silence_duration: 0.021\n"
            "[silencedetect] silence_start: 2.000\n"
            "[silencedetect] silence_end: 2.021 | silence_duration: 0.021\n"
        ),
    )
    assert state.max_silence_seconds == pytest.approx(0.021)
    assert state.total_silence_seconds == pytest.approx(0.042)
    assert state.silence_events == 2


def test_decoded_diagnostics_measure_silence_without_losing_pcm() -> None:
    state = StreamState(
        label="test",
        url="http://example.test/stream",
        process=SimpleNamespace(),  # type: ignore[arg-type]
        started_monotonic=0.0,
        decoded_audio_mode=True,
    )
    _read_decoded_audio(state, io.BytesIO(bytes(192_000)))
    _read_diagnostics(
        state,
        io.StringIO(
            "[silencedetect] silence_start: 0.0\n"
            "[silencedetect] silence_end: 1.0 | silence_duration: 1.0\n"
        ),
    )
    assert state.decoded_audio_bytes_total == 192_000
    assert state.media_seconds == pytest.approx(1.0)
    assert state.total_silence_seconds == pytest.approx(1.0)
    assert state.silence_events == 1


def test_reader_retry_keeps_failed_evidence_and_current_generation_scope(monkeypatch):
    from tools import monitor_stream_continuity as monitor

    old = StreamState("test", "http://example.test/audio", SimpleNamespace(poll=lambda: 17), 0.0, decoded_audio_mode=True)
    old.media_seconds = 10.0
    old.decoded_audio_bytes_total = 10 * 192000
    old.first_progress_monotonic = 0.1
    old.last_progress_monotonic = 10.0
    _begin_measurement(old, 10.0, listener_buffer_seconds=4.0)
    states = [old]
    metrics = {"test": _empty_evaluation_metrics()}
    events = []
    monkeypatch.setattr(monitor, "_write_json_line", lambda handle, value: events.append(value))
    replacement = StreamState("test", old.url, SimpleNamespace(poll=lambda: None), 25.0, decoded_audio_mode=True)
    monkeypatch.setattr(monitor, "_start_stream", lambda *args, **kwargs: replacement)
    _retry_exited_readers(states, Path("ffmpeg.exe"), 20.0, metrics, io.StringIO(), measuring=True)
    assert states[0] is old
    assert old.reader_restart_due_monotonic == 25.0
    _retry_exited_readers(states, Path("ffmpeg.exe"), 24.0, metrics, io.StringIO(), measuring=True)
    assert states[0] is old
    _retry_exited_readers(states, Path("ffmpeg.exe"), 25.0, metrics, io.StringIO(), measuring=True)
    assert states[0] is replacement
    assert replacement.reader_restart_count == 1
    assert replacement.reader_previous_decoded_audio_bytes == 10 * 192000
    snapshot = _snapshot(replacement, 25.0)
    assert snapshot["process_alive"] is True
    assert snapshot["exit_code"] is None
    assert snapshot["last_unexpected_exit_code"] == 17
    assert snapshot["unexpected_exit"] is True
    assert snapshot["readiness_satisfied"] is False
    assert metrics["test"]["unexpected_exit"] is True
    assert metrics["test"]["exit_codes"] == {17}
    assert [v["type"] for v in events] == ["continuity_reader_exit", "continuity_reader_restarted"]


def test_warmup_reader_failure_is_not_erased_by_retry_backoff(monkeypatch):
    from tools import monitor_stream_continuity as monitor

    old = StreamState("test", "http://example.test/audio", SimpleNamespace(poll=lambda: 0), 0.0, decoded_audio_mode=True)
    old.reader_restart_count = 12
    metrics = {"test": _empty_evaluation_metrics()}
    monkeypatch.setattr(monitor, "_write_json_line", lambda *args: None)
    _retry_exited_readers([old], Path("ffmpeg.exe"), 10.0, metrics, io.StringIO(), measuring=False)
    assert old.reader_restart_due_monotonic == 70.0
    assert metrics["test"]["unexpected_exit"] is True
    assert metrics["test"]["exit_codes"] == {0}


def test_evaluate_fails_real_playback_deficit() -> None:
    snapshots = [
        {
            "elapsed_seconds": 12.0,
            "playback_margin_seconds": -6.0,
            "progress_age_seconds": 1.0,
            "max_silence_seconds": 0.0,
            "unexpected_exit": False,
            "transport_errors": 0,
        }
    ]
    result = _evaluate(
        snapshots,
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=15.0,
        maximum_silence_seconds=15.0,
    )
    assert result["continuity_ok"] is False


def test_evaluate_reports_no_progress_before_ten_second_margin_window() -> None:
    result = _evaluate(
        [
            {
                "elapsed_seconds": 4.0,
                "playback_margin_seconds": None,
                "progress_age_seconds": 4.0,
                "max_progress_gap_seconds": 0.0,
                "max_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            }
        ],
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=3.0,
        maximum_silence_seconds=0.25,
    )
    assert result["continuity_ok"] is False
    assert result["maximum_progress_age_seconds"] == pytest.approx(4.0)


def test_evaluate_fails_cumulative_short_silences() -> None:
    result = _evaluate(
        [
            {
                "elapsed_seconds": 12.0,
                "playback_margin_seconds": 0.0,
                "progress_age_seconds": 0.1,
                "max_progress_gap_seconds": 0.5,
                "max_silence_seconds": 0.03,
                "total_silence_seconds": 0.75,
                "unexpected_exit": False,
                "transport_errors": 0,
            }
        ],
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=5.0,
        maximum_silence_seconds=0.25,
        maximum_progress_gap_seconds=2.0,
        maximum_total_silence_seconds=0.5,
    )
    assert result["continuity_ok"] is False
    assert result["total_silence_seconds"] == pytest.approx(0.75)


def test_evaluate_ignores_warmup_gaps_but_reports_startup() -> None:
    result = _evaluate(
        [
            {
                "elapsed_seconds": 8.0,
                "measurement_active": False,
                "playback_margin_seconds": -10.0,
                "progress_age_seconds": 8.0,
                "max_progress_gap_seconds": 8.0,
                "max_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            },
            {
                "elapsed_seconds": 2.0,
                "measurement_active": True,
                "startup_delay_seconds": 4.0,
                "startup_progress_age_seconds": 0.2,
                "startup_transport_errors": 0,
                "readiness_satisfied": True,
                "playback_margin_seconds": -0.2,
                "progress_age_seconds": 0.3,
                "max_progress_gap_seconds": 1.2,
                "max_silence_seconds": 0.0,
                "total_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            },
        ],
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=5.0,
        maximum_silence_seconds=0.25,
        maximum_progress_gap_seconds=2.0,
        maximum_total_silence_seconds=0.5,
        maximum_startup_delay_seconds=15.0,
    )
    assert result["continuity_ok"] is True
    assert result["startup_ok"] is True
    assert result["maximum_progress_gap_seconds"] == pytest.approx(1.2)


def test_strict_audio_evaluation_uses_pcm_gaps_not_silence_or_progress_ticks() -> None:
    snapshots = [
        {
            "elapsed_seconds": 12.0,
            "measurement_active": True,
            "startup_delay_seconds": 1.0,
            "startup_progress_age_seconds": 0.02,
            "startup_transport_errors": 0,
            "readiness_satisfied": True,
            "playback_margin_seconds": 0.0,
            "progress_age_seconds": 0.02,
            "max_progress_gap_seconds": 1.0,
            "decoded_audio_gap_seconds": 0.04,
            "decoded_audio_seconds": 12.0,
            "max_silence_seconds": 10.0,
            "total_silence_seconds": 30.0,
            "unexpected_exit": False,
            "transport_errors": 0,
        }
    ]
    result = _evaluate(
        snapshots,
        minimum_margin_seconds=0.0,
        maximum_progress_age_seconds=0.5,
        maximum_silence_seconds=0.0,
        maximum_progress_gap_seconds=0.0,
        maximum_total_silence_seconds=0.0,
        maximum_startup_delay_seconds=5.0,
        decoded_audio_mode=True,
        maximum_decoded_audio_gap_seconds=0.1,
    )
    assert result["continuity_ok"] is True
    assert result["maximum_decoded_audio_gap_seconds"] == pytest.approx(0.04)


def test_strict_decoded_audio_evaluation_fails_when_pcm_output_stalls() -> None:
    result = _evaluate(
        [
            {
                "measurement_active": True,
                "startup_delay_seconds": 1.0,
                "startup_progress_age_seconds": 0.02,
                "startup_transport_errors": 0,
                "readiness_satisfied": True,
                "playback_margin_seconds": 0.0,
                "progress_age_seconds": 0.8,
                "max_progress_gap_seconds": 0.8,
                "decoded_audio_gap_seconds": 0.8,
                "decoded_audio_seconds": 10.0,
                "max_silence_seconds": 0.0,
                "total_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            }
        ],
        minimum_margin_seconds=0.0,
        maximum_progress_age_seconds=1.0,
        maximum_silence_seconds=0.0,
        maximum_progress_gap_seconds=0.0,
        maximum_total_silence_seconds=0.0,
        maximum_startup_delay_seconds=5.0,
        decoded_audio_mode=True,
        maximum_decoded_audio_gap_seconds=0.25,
    )
    assert result["continuity_ok"] is False


def test_evaluate_fails_when_startup_exceeds_deadline() -> None:
    result = _evaluate(
        [
            {
                "elapsed_seconds": 1.0,
                "measurement_active": True,
                "startup_delay_seconds": 17.0,
                "startup_progress_age_seconds": 0.1,
                "startup_transport_errors": 0,
                "readiness_satisfied": True,
                "playback_margin_seconds": -0.1,
                "progress_age_seconds": 0.1,
                "max_progress_gap_seconds": 0.1,
                "max_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            }
        ],
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=5.0,
        maximum_silence_seconds=0.25,
        maximum_startup_delay_seconds=15.0,
    )
    assert result["continuity_ok"] is False
    assert result["startup_ok"] is False


def test_evaluate_fails_when_readiness_never_stabilizes() -> None:
    result = _evaluate(
        [
            {
                "elapsed_seconds": 1.0,
                "measurement_active": True,
                "startup_delay_seconds": 2.0,
                "startup_progress_age_seconds": 0.1,
                "startup_transport_errors": 0,
                "readiness_satisfied": False,
                "playback_margin_seconds": -0.1,
                "progress_age_seconds": 0.1,
                "max_progress_gap_seconds": 0.1,
                "max_silence_seconds": 0.0,
                "unexpected_exit": False,
                "transport_errors": 0,
            }
        ],
        minimum_margin_seconds=-5.0,
        maximum_progress_age_seconds=5.0,
        maximum_silence_seconds=0.25,
    )
    assert result["continuity_ok"] is False
    assert result["startup_ok"] is False


def test_begin_measurement_resets_pre_window_metrics() -> None:
    state = StreamState(
        label="test",
        url="http://example.test/stream",
        process=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        started_monotonic=10.0,
        media_seconds=25.0,
        first_progress_monotonic=12.0,
        first_media_seconds=1.0,
        last_progress_monotonic=19.5,
        max_silence_seconds=1.0,
        total_silence_seconds=2.0,
        silence_events=3,
        transport_errors=4,
    )
    result = _begin_measurement(state, 20.0)
    assert result["startup_delay_seconds"] == pytest.approx(2.0)
    assert result["startup_progress_age_seconds"] == pytest.approx(0.5)
    assert result["startup_transport_errors"] == 4
    assert state.measurement_media_baseline == pytest.approx(25.0)
    assert state.measurement_last_progress_monotonic == pytest.approx(19.5)
    assert state.measurement_max_progress_gap_seconds == 0.0
    assert state.max_silence_seconds == 0.0
    assert state.total_silence_seconds == 0.0
    assert state.silence_events == 0
    assert state.transport_errors == 0
