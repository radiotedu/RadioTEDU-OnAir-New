import os
from urllib.parse import quote

from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile
from app.audio.virtual_sources import is_silence_input_uri

LOCAL_PCM_FORMAT = "s16le"
LOCAL_PCM_CODEC = "pcm_s16le"
LOCAL_PCM_SAMPLE_RATE = 48000
LOCAL_PCM_CHANNELS = 2
LOCAL_MONITOR_INITIAL_BURST_SECONDS = 10.0
LOCAL_MONITOR_CATCHUP_RATE = 2.0
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _normalize_metadata_value(value: str) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _format_seconds(value: float) -> str:
    return f"{max(0.0, float(value)):.3f}"


def _icecast_output_url(cfg: StationPipelineConfig) -> str:
    mount = cfg.icecast_mount if cfg.icecast_mount.startswith("/") else f"/{cfg.icecast_mount}"
    user = quote(str(cfg.icecast_user or ""), safe="")
    password = quote(str(cfg.icecast_password or ""), safe="")
    port = int(cfg.icecast_port)
    return f"icecast://{user}:{password}@{cfg.icecast_host}:{port}{mount}"


def _append_track_metadata(cmd: list[str], cfg: StationPipelineConfig) -> None:
    if any(
        str(os.getenv(name, "")).strip().lower() in _TRUTHY_ENV_VALUES
        for name in (
            "CLEANROOM_SKIP_STREAM_METADATA",
            "CLEANROOM_SKIP_ICECAST_METADATA",
        )
    ):
        return
    stream_title = _normalize_metadata_value(cfg.stream_title)
    stream_artist = _normalize_metadata_value(cfg.stream_artist)
    if stream_title:
        cmd.extend(["-metadata", f"title={stream_title}"])
    if stream_artist:
        cmd.extend(["-metadata", f"artist={stream_artist}"])


def _pcm_output_args() -> list[str]:
    return [
        "-c:a",
        LOCAL_PCM_CODEC,
        "-f",
        LOCAL_PCM_FORMAT,
        "-ar",
        str(LOCAL_PCM_SAMPLE_RATE),
        "-ac",
        str(LOCAL_PCM_CHANNELS),
    ]


def _icecast_output_args(cfg: StationPipelineConfig) -> list[str]:
    profile = resolve_stream_profile(cfg.stream_codec_profile, cfg.stream_bitrate_kbps)
    args: list[str] = []
    filters: list[str] = []
    profile_filters = [str(item) for item in profile.get("ffmpeg_filter_args", [])]
    for index, item in enumerate(profile_filters):
        if item == "-af" and index + 1 < len(profile_filters):
            filters.append(profile_filters[index + 1])
    loudness_target = getattr(cfg, "loudness_target_lufs", None)
    if loudness_target is not None:
        target = max(-24.0, min(-9.0, float(loudness_target)))
        filters.append(f"loudnorm=I={target:.1f}:TP=-1.5:LRA=11")
    if abs(float(cfg.output_gain_db or 0.0)) > 0.001:
        filters.append(f"volume={float(cfg.output_gain_db):.2f}dB")
    if filters:
        args.extend(["-af", ",".join(filters)])
    args.extend(["-ar", str(LOCAL_PCM_SAMPLE_RATE), "-ac", str(LOCAL_PCM_CHANNELS)])
    args.extend(["-c:a", str(profile["ffmpeg_codec"])])
    bitrate_kbps = int(profile.get("bitrate_kbps") or 0)
    if bool(profile.get("uses_bitrate", True)) and bitrate_kbps > 0:
        args.extend(["-b:a", f"{bitrate_kbps}k"])
    ffmpeg_profile = str(profile.get("ffmpeg_profile") or "").strip()
    if ffmpeg_profile:
        args.extend(["-profile:a", ffmpeg_profile])
    args.extend(str(item) for item in profile.get("ffmpeg_encoder_args", []))
    format_name = str(profile["format"])
    if format_name == "ogg":
        args.extend(["-page_duration", "20000", "-flush_packets", "1"])
    args.extend(
        [
            "-content_type",
            str(profile["content_type"]),
            "-f",
            format_name,
        ]
    )
    return args


def _icecast_protocol_args(cfg: StationPipelineConfig) -> list[str]:
    args: list[str] = []
    if bool(getattr(cfg, "icecast_tls_enabled", False)):
        args.extend(["-tls", "1"])
    if bool(getattr(cfg, "icecast_legacy_source_enabled", False)):
        args.extend(["-legacy_icecast", "1"])
    user_agent = _normalize_metadata_value(
        str(getattr(cfg, "icecast_user_agent", "") or "")
    )
    if user_agent:
        args.extend(["-user_agent", user_agent])
    name = _normalize_metadata_value(
        str(getattr(cfg, "icecast_stream_name", "") or getattr(cfg, "station_name", "") or "")
    )
    description = _normalize_metadata_value(
        str(getattr(cfg, "icecast_description", "") or "")
    )
    genre = _normalize_metadata_value(str(getattr(cfg, "icecast_genre", "") or ""))
    if name:
        args.extend(["-ice_name", name])
    if description:
        args.extend(["-ice_description", description])
    if genre:
        args.extend(["-ice_genre", genre])
    args.extend(["-ice_public", "1" if bool(getattr(cfg, "icecast_public", True)) else "0"])
    return args


def _input_pacing_args(
    *,
    realtime: bool,
    initial_burst_seconds: float = 0.0,
    catchup_rate: float | None = None,
) -> list[str]:
    if not realtime:
        return []
    args = ["-readrate", "1"]
    if float(initial_burst_seconds or 0.0) > 0.0:
        args.extend(["-readrate_initial_burst", _format_seconds(initial_burst_seconds)])
    if catchup_rate is not None and float(catchup_rate) > 1.0:
        args.extend(["-readrate_catchup", f"{float(catchup_rate):.3f}"])
    return args


def _silence_filter_spec() -> str:
    return "anullsrc=r=48000:cl=stereo"


FAST_AUDIO_CACHE_DIR = r"C:\ProgramData\RadioTEDU\OnAir\FastAudioCache"


def _resolve_fast_cached_uri(input_uri: str) -> str:
    if not input_uri or not os.path.exists(input_uri):
        return input_uri
    drive, _ = os.path.splitdrive(input_uri)
    if drive and drive.upper() == "C:":
        return input_uri
    try:
        os.makedirs(FAST_AUDIO_CACHE_DIR, exist_ok=True)
        filename = os.path.basename(input_uri)
        import hashlib, shutil
        file_hash = hashlib.md5(input_uri.encode("utf-8")).hexdigest()[:8]
        cached_name = f"{file_hash}_{filename}"
        cached_path = os.path.join(FAST_AUDIO_CACHE_DIR, cached_name)
        if os.path.exists(cached_path) and os.path.getsize(cached_path) == os.path.getsize(input_uri):
            return cached_path
        total_size = sum(os.path.getsize(os.path.join(FAST_AUDIO_CACHE_DIR, f)) for f in os.listdir(FAST_AUDIO_CACHE_DIR) if os.path.isfile(os.path.join(FAST_AUDIO_CACHE_DIR, f)))
        if total_size > 400 * 1024 * 1024:
            cached_files = sorted([os.path.join(FAST_AUDIO_CACHE_DIR, f) for f in os.listdir(FAST_AUDIO_CACHE_DIR)], key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
            for old_f in cached_files[:10]:
                try: os.remove(old_f)
                except: pass
        shutil.copy2(input_uri, cached_path)
        return cached_path
    except Exception:
        return input_uri


def _build_input_args(
    input_uri: str,
    *,
    realtime: bool,
    initial_burst_seconds: float = 0.0,
    catchup_rate: float | None = None,
    start_offset_seconds: float = 0.0,
) -> list[str]:
    if is_silence_input_uri(input_uri):
        args: list[str] = []
        if realtime:
            args.append("-re")
        args.extend(["-f", "lavfi", "-i", _silence_filter_spec()])
        return args

    effective_uri = _resolve_fast_cached_uri(input_uri)
    args: list[str] = [
        "-thread_queue_size", "8192",
        "-probesize", "10000000",
        "-analyzeduration", "10000000",
    ]
    if float(start_offset_seconds or 0.0) > 0.0:
        args.extend(["-ss", _format_seconds(start_offset_seconds)])
    args.extend(
        [
            *_input_pacing_args(
                realtime=realtime,
                initial_burst_seconds=initial_burst_seconds,
                catchup_rate=catchup_rate,
            ),
            "-i",
            effective_uri,
        ]
    )
    return args


def build_ffmpeg_icecast_cmd(cfg: StationPipelineConfig, ffmpeg_bin: str) -> list[str]:
    out_url = _icecast_output_url(cfg)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *_build_input_args(cfg.input_uri, realtime=True),
        "-vn",
        *_icecast_output_args(cfg),
        *_icecast_protocol_args(cfg),
    ]
    _append_track_metadata(cmd, cfg)
    cmd.append(out_url)
    return cmd


def build_ffmpeg_icecast_sink_cmd(cfg: StationPipelineConfig, ffmpeg_bin: str) -> list[str]:
    out_url = _icecast_output_url(cfg)
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        LOCAL_PCM_FORMAT,
        "-ar",
        str(LOCAL_PCM_SAMPLE_RATE),
        "-ac",
        str(LOCAL_PCM_CHANNELS),
        "-i",
        "pipe:0",
        "-vn",
        *_icecast_output_args(cfg),
        *_icecast_protocol_args(cfg),
        out_url,
    ]


def build_ffmpeg_encoded_sink_cmd(
    cfg: StationPipelineConfig,
    ffmpeg_bin: str,
) -> list[str]:
    """Encode interleaved PCM to stdout for a protocol adapter.

    Credentials and destination details deliberately stay out of this command.
    The transport adapter owns authentication and bounded socket I/O.
    """

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        LOCAL_PCM_FORMAT,
        "-ar",
        str(LOCAL_PCM_SAMPLE_RATE),
        "-ac",
        str(LOCAL_PCM_CHANNELS),
        "-i",
        "pipe:0",
        "-vn",
        *_icecast_output_args(cfg),
        "pipe:1",
    ]


def build_ffmpeg_pcm_producer_cmd(
    cfg: StationPipelineConfig,
    ffmpeg_bin: str,
    start_offset_seconds: float = 0.0,
    realtime: bool = True,
    initial_burst_seconds: float = 0.0,
    catchup_rate: float | None = None,
) -> list[str]:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    cmd.extend(
        [
            *_build_input_args(
                cfg.input_uri,
                realtime=realtime,
                initial_burst_seconds=initial_burst_seconds,
                catchup_rate=catchup_rate,
                start_offset_seconds=start_offset_seconds,
            ),
            "-vn",
            *_pcm_output_args(),
            "pipe:1",
        ]
    )
    return cmd


def build_ffmpeg_local_pcm_cmd(
    cfg: StationPipelineConfig,
    ffmpeg_bin: str,
    start_offset_seconds: float = 0.0,
) -> list[str]:
    return build_ffmpeg_pcm_producer_cmd(
        cfg,
        ffmpeg_bin,
        start_offset_seconds=start_offset_seconds,
        realtime=True,
        initial_burst_seconds=LOCAL_MONITOR_INITIAL_BURST_SECONDS,
        catchup_rate=LOCAL_MONITOR_CATCHUP_RATE,
    )


def _build_ffmpeg_crossfade_base_cmd(
    current_cfg: StationPipelineConfig,
    next_cfg: StationPipelineConfig,
    ffmpeg_bin: str,
    current_offset_seconds: float,
    *,
    realtime: bool = True,
    initial_burst_seconds: float = 0.0,
    catchup_rate: float | None = None,
) -> list[str]:
    seconds = _format_seconds(next_cfg.crossfade_seconds)
    filter_graph = (
        f"[0:a]atrim=0:{seconds},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st=0:d={seconds}[current_xf];"
        f"[1:a]asplit=2[next_head][next_tail];"
        f"[next_head]atrim=0:{seconds},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={seconds}[next_xf];"
        "[current_xf][next_xf]amix=inputs=2:duration=longest:normalize=0[mixed];"
        f"[next_tail]atrim=start={seconds},asetpts=PTS-STARTPTS[tail];"
        "[mixed][tail]concat=n=2:v=0:a=1[outa]"
    )
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *_build_input_args(
            current_cfg.input_uri,
            realtime=realtime,
            initial_burst_seconds=initial_burst_seconds,
            catchup_rate=catchup_rate,
            start_offset_seconds=current_offset_seconds,
        ),
        *_build_input_args(
            next_cfg.input_uri,
            realtime=realtime,
            initial_burst_seconds=initial_burst_seconds,
            catchup_rate=catchup_rate,
        ),
        "-filter_complex",
        filter_graph,
        "-vn",
    ]
    return cmd


def build_ffmpeg_crossfade_cmd(
    current_cfg: StationPipelineConfig,
    next_cfg: StationPipelineConfig,
    ffmpeg_bin: str,
    current_offset_seconds: float,
    include_local_pipe: bool = False,
    realtime: bool = True,
    initial_burst_seconds: float = 0.0,
    catchup_rate: float | None = None,
) -> list[str]:
    cmd = _build_ffmpeg_crossfade_base_cmd(
        current_cfg,
        next_cfg,
        ffmpeg_bin,
        current_offset_seconds,
        realtime=realtime,
        initial_burst_seconds=initial_burst_seconds,
        catchup_rate=catchup_rate,
    )
    wrote_output = False
    if next_cfg.icecast_enabled:
        cmd.extend(
            [
                "-map",
                "[outa]",
                *_icecast_output_args(next_cfg),
            ]
        )
        _append_track_metadata(cmd, next_cfg)
        cmd.append(_icecast_output_url(next_cfg))
        wrote_output = True
    if include_local_pipe:
        cmd.extend(
            [
                "-map",
                "[outa]",
                *_pcm_output_args(),
                "pipe:1",
            ]
        )
        wrote_output = True
    if not wrote_output:
        raise ValueError("at least one transition output target must be enabled")
    return cmd


def build_ffmpeg_crossfade_pcm_cmd(
    current_cfg: StationPipelineConfig,
    next_cfg: StationPipelineConfig,
    ffmpeg_bin: str,
    current_offset_seconds: float,
    *,
    realtime: bool = True,
    initial_burst_seconds: float = LOCAL_MONITOR_INITIAL_BURST_SECONDS,
    catchup_rate: float | None = LOCAL_MONITOR_CATCHUP_RATE,
) -> list[str]:
    return [
        *_build_ffmpeg_crossfade_base_cmd(
            current_cfg,
            next_cfg,
            ffmpeg_bin,
            current_offset_seconds,
            realtime=realtime,
            initial_burst_seconds=initial_burst_seconds,
            catchup_rate=catchup_rate,
        ),
        "-map",
        "[outa]",
        *_pcm_output_args(),
        "pipe:1",
    ]


def build_ffplay_local_cmd(cfg: StationPipelineConfig, ffplay_bin: str) -> list[str]:
    window_title = str(cfg.station_name or "").strip() or "RadioTEDU OnAir"
    return [
        ffplay_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-window_title",
        window_title,
        "-nodisp",
        "-autoexit",
        "-infbuf",
        "-f",
        LOCAL_PCM_FORMAT,
        "-ar",
        str(LOCAL_PCM_SAMPLE_RATE),
        "-ch_layout",
        "stereo",
        "-i",
        "pipe:0",
    ]
