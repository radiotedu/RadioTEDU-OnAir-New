from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.monitor_stream_continuity import (
    _begin_measurement,
    _evaluate,
    _parse_clock,
    _parse_stream_arg,
    _read_diagnostics,
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
