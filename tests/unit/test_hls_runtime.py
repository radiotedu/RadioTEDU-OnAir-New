from pathlib import Path

from app.services.hls_runtime import (
    HLS_CODEC_PROFILE,
    HLS_HIGH_BITRATE_KBPS,
    HLS_LOW_BITRATE_KBPS,
    build_ffmpeg_args,
)


def test_hls_command_is_he_aac_only_and_has_two_variants():
    args = build_ffmpeg_args(
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "http://127.0.0.1:11154/lofi",
        r"D:\RadioTEDU-HLS",
        "lofi",
    )
    assert HLS_CODEC_PROFILE == "he_aac_v1_96_192"
    assert "libfdk_aac" in args
    assert "aac_he" in args
    assert f"{HLS_LOW_BITRATE_KBPS}k" in args
    assert f"{HLS_HIGH_BITRATE_KBPS}k" in args
    assert "libopus" not in args
    assert args[args.index("-var_stream_map") + 1] == "a:0,name:low a:1,name:high"


def test_hls_command_writes_radio_scoped_playlists_and_segments():
    args = build_ffmpeg_args(
        "ffmpeg.exe",
        "http://stream.radiotedu.com:11154/radio",
        Path(r"D:\RadioTEDU-HLS"),
        "radio",
    )
    segment_pattern = args[args.index("-hls_segment_filename") + 1]
    output_playlist = args[-1]
    assert r"radio\%v\segment_%09d.ts" in segment_pattern
    assert r"radio\%v\index.m3u8" in output_playlist
