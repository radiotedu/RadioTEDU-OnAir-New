from __future__ import annotations

from pathlib import Path

import pytest

from tools.monitor_stream_continuity import (
    _evaluate,
    _parse_clock,
    _parse_stream_arg,
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
    assert any(value.startswith("silencedetect=") for value in command)


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
