from app.audio.ffmpeg_pipeline import (
    build_ffmpeg_crossfade_cmd,
    build_ffmpeg_crossfade_pcm_cmd,
    build_ffmpeg_icecast_cmd,
    build_ffmpeg_icecast_sink_cmd,
    build_ffmpeg_local_pcm_cmd,
    build_ffplay_local_cmd,
)
from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile


def _cfg(
    title: str = "",
    artist: str = "",
    input_uri: str = "C:/music/demo.mp3",
    crossfade_seconds: float = 3.0,
    local_output_enabled: bool = False,
    stream_codec_profile: str = "aac_plus_196",
) -> StationPipelineConfig:
    return StationPipelineConfig(
        input_uri=input_uri,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/live",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=local_output_enabled,
        output_device_id="",
        icecast_enabled=True,
        stream_title=title,
        stream_artist=artist,
        crossfade_seconds=crossfade_seconds,
        stream_codec_profile=stream_codec_profile,
        stream_bitrate_kbps=128 if stream_codec_profile == "mp3_128" else 196,
    )


def test_ffmpeg_command_includes_track_metadata_when_available() -> None:
    cmd = build_ffmpeg_icecast_cmd(_cfg(title="Song A", artist="Artist B"), "ffmpeg.exe")
    assert "-metadata" in cmd
    assert "title=Song A" in cmd
    assert "artist=Artist B" in cmd
    assert "-content_type" in cmd
    assert "audio/aac" in cmd
    assert "-f" in cmd
    assert "adts" in cmd
    assert "196k" in cmd


def test_ffmpeg_command_omits_empty_track_metadata() -> None:
    cmd = build_ffmpeg_icecast_cmd(_cfg(title="", artist=""), "ffmpeg.exe")
    joined = " ".join(cmd)
    assert "title=" not in joined
    assert "artist=" not in joined


def test_true_aac_plus_profile_requires_fdk_he_aac_encoder() -> None:
    profile = resolve_stream_profile("he_aac_96", 96)
    cmd = build_ffmpeg_icecast_sink_cmd(_cfg(stream_codec_profile="he_aac_96"), "ffmpeg.exe")

    assert profile["ffmpeg_codec"] == "libfdk_aac"
    assert profile["ffmpeg_profile"] == "aac_he"
    assert profile["profile"] == "he_aac_96"
    assert "libfdk_aac" in cmd
    assert "aac_he" in cmd
    assert "96k" in cmd
    assert "-afterburner" in cmd


def test_ffmpeg_command_omits_track_metadata_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("CLEANROOM_SKIP_STREAM_METADATA", "1")
    cmd = build_ffmpeg_icecast_cmd(
        _cfg(title="Private Song", artist="Private Artist"),
        "ffmpeg.exe",
    )
    joined = " ".join(cmd)
    assert "title=Private Song" not in joined
    assert "artist=Private Artist" not in joined


def test_ffmpeg_command_omits_track_metadata_for_station_policy() -> None:
    cfg = _cfg(title="Lo-Fi title", artist="Lo-Fi artist")
    cfg.metadata_suppressed = True
    cmd = build_ffmpeg_icecast_cmd(cfg, "ffmpeg.exe")
    joined = " ".join(cmd)
    assert "title=Lo-Fi title" not in joined
    assert "artist=Lo-Fi artist" not in joined


def test_ffmpeg_command_supports_internal_silence_source() -> None:
    cmd = build_ffmpeg_icecast_cmd(_cfg(input_uri="silence://continuous"), "ffmpeg.exe")

    joined = " ".join(cmd)
    assert "-f lavfi" in joined
    assert "anullsrc=r=48000:cl=stereo" in joined
    assert "silence://continuous" not in joined


def test_build_ffmpeg_crossfade_cmd_seeks_current_input_and_mixes_immediately() -> None:
    current_cfg = _cfg(title="Current Song", artist="Current Artist", input_uri="C:/music/current.mp3")
    next_cfg = _cfg(title="Next Song", artist="Next Artist", input_uri="C:/music/next.mp3")

    cmd = build_ffmpeg_crossfade_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin="ffmpeg.exe",
        current_offset_seconds=12.5,
        include_local_pipe=False,
    )

    joined = " ".join(cmd)
    assert "-ss" in cmd
    assert "12.500" in joined
    assert "C:/music/current.mp3" in joined
    assert "C:/music/next.mp3" in joined
    assert "afade" in joined
    assert "amix" in joined
    assert "concat" in joined
    assert "icecast://" in joined
    assert "title=Next Song" in joined
    assert "artist=Next Artist" in joined


def test_build_ffmpeg_crossfade_cmd_can_include_local_pipe_output() -> None:
    current_cfg = _cfg(input_uri="C:/music/current.mp3")
    next_cfg = _cfg(input_uri="C:/music/next.mp3", local_output_enabled=True)

    cmd = build_ffmpeg_crossfade_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin="ffmpeg.exe",
        current_offset_seconds=3.0,
        include_local_pipe=True,
    )

    joined = " ".join(cmd)
    assert "pipe:1" in joined


def test_build_ffmpeg_crossfade_cmd_uses_separate_codecs_for_icecast_and_local_pipe() -> None:
    current_cfg = _cfg(input_uri="C:/music/current.mp3", local_output_enabled=True)
    next_cfg = _cfg(input_uri="C:/music/next.mp3", local_output_enabled=True)

    cmd = build_ffmpeg_crossfade_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin="ffmpeg.exe",
        current_offset_seconds=3.0,
        include_local_pipe=True,
    )

    joined = " ".join(cmd)
    assert "icecast://" in joined
    assert "pipe:1" in joined
    assert "-content_type audio/aac" in joined
    assert "-f adts" in joined
    assert "pcm_s16le" in joined


def test_build_ffmpeg_icecast_cmd_supports_mp3_profile() -> None:
    cmd = build_ffmpeg_icecast_cmd(
        _cfg(title="Song A", artist="Artist B", stream_codec_profile="mp3_128"),
        "ffmpeg.exe",
    )

    joined = " ".join(cmd)
    assert "-c:a libmp3lame" in joined
    assert "-b:a 128k" in joined
    assert "-content_type audio/mpeg" in joined
    assert "-f mp3" in joined


def test_build_ffmpeg_crossfade_cmd_uses_raw_pcm_for_local_pipe_output() -> None:
    current_cfg = _cfg(input_uri="C:/music/current.mp3", local_output_enabled=True)
    current_cfg.icecast_enabled = False
    next_cfg = _cfg(input_uri="C:/music/next.mp3", local_output_enabled=True)
    next_cfg.icecast_enabled = False

    cmd = build_ffmpeg_crossfade_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin="ffmpeg.exe",
        current_offset_seconds=3.0,
        include_local_pipe=True,
    )

    joined = " ".join(cmd)
    assert "pipe:1" in joined
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ac 2" in joined
    assert "-f wav" not in joined


def test_build_ffmpeg_icecast_sink_cmd_reads_raw_pcm_from_stdin() -> None:
    cmd = build_ffmpeg_icecast_sink_cmd(_cfg(title="Song A", artist="Artist B"), "ffmpeg.exe")

    joined = " ".join(cmd)
    assert "-i pipe:0" in joined
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ac 2" in joined
    assert "icecast://" in joined
    assert "-content_type audio/aac" in joined
    assert "-f adts" in joined
    assert "title=Song A" not in joined
    assert "artist=Artist B" not in joined


def test_nonstandard_port_does_not_force_legacy_icecast_protocol() -> None:
    cfg = _cfg()
    cfg.icecast_port = 11154
    cmd = build_ffmpeg_icecast_sink_cmd(cfg, "ffmpeg.exe")
    assert "-legacy_icecast" not in cmd


def test_legacy_icecast_protocol_requires_explicit_station_setting() -> None:
    cfg = _cfg()
    cfg.icecast_legacy_source_enabled = True
    cmd = build_ffmpeg_icecast_sink_cmd(cfg, "ffmpeg.exe")
    assert cmd[cmd.index("-legacy_icecast") + 1] == "1"


def test_build_ffmpeg_local_pcm_cmd_writes_raw_pcm_to_stdout() -> None:
    cmd = build_ffmpeg_local_pcm_cmd(_cfg(input_uri="C:/music/current.mp3"), "ffmpeg.exe")

    joined = " ".join(cmd)
    assert "C:/music/current.mp3" in joined
    assert "-readrate 1" in joined
    assert "-readrate_initial_burst 10.000" in joined
    assert "-readrate_catchup 2.000" in joined
    assert "pipe:1" in joined
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ac 2" in joined
    assert "icecast://" not in joined


def test_build_ffmpeg_local_pcm_cmd_supports_internal_silence_source() -> None:
    cmd = build_ffmpeg_local_pcm_cmd(_cfg(input_uri="silence://continuous"), "ffmpeg.exe")

    joined = " ".join(cmd)
    assert "-re" in cmd
    assert "-f lavfi" in joined
    assert "anullsrc=r=48000:cl=stereo" in joined
    assert "pipe:1" in joined


def test_build_ffmpeg_crossfade_pcm_cmd_writes_raw_pcm_without_icecast_delivery() -> None:
    current_cfg = _cfg(input_uri="C:/music/current.mp3")
    next_cfg = _cfg(input_uri="C:/music/next.mp3")

    cmd = build_ffmpeg_crossfade_pcm_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin="ffmpeg.exe",
        current_offset_seconds=3.0,
    )

    joined = " ".join(cmd)
    assert "C:/music/current.mp3" in joined
    assert "C:/music/next.mp3" in joined
    assert "pipe:1" in joined
    assert "-readrate_initial_burst 10.000" in joined
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ac 2" in joined
    assert "icecast://" not in joined
    assert "title=" not in joined
    assert "artist=" not in joined


def test_build_ffplay_local_cmd_uses_raw_pcm_pipe_input() -> None:
    cfg = _cfg(input_uri="C:/music/current.mp3", local_output_enabled=True)

    cmd = build_ffplay_local_cmd(cfg, "ffplay.exe")

    joined = " ".join(cmd)
    assert "-autoexit" in cmd
    assert "-infbuf" in cmd
    assert "-loop" not in cmd
    assert "-f s16le" in joined
    assert "-ar 48000" in joined
    assert "-ch_layout stereo" in joined
    assert "pipe:0" in joined
    assert "C:/music/current.mp3" not in joined
