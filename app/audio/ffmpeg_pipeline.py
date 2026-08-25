import os
import threading
import hashlib
import logging
import shutil
import stat
import time
from urllib.parse import quote

from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile
from app.audio.virtual_sources import is_silence_input_uri
from app.services.track_naming import clean_album_metadata

LOCAL_PCM_FORMAT = "s16le"
LOCAL_PCM_CODEC = "pcm_s16le"
LOCAL_PCM_SAMPLE_RATE = 48000
LOCAL_PCM_CHANNELS = 2
LOCAL_MONITOR_INITIAL_BURST_SECONDS = 10.0
LOCAL_MONITOR_CATCHUP_RATE = 2.0
# ITU-R BS.1770 defines the loudness/true-peak measurement algorithm.  EBU
# R128 supplies the operational broadcast target used with it.
ITU_PROGRAM_LOUDNESS_LUFS = -23.0
ITU_TRUE_PEAK_DBTP = -1.0
# BS.1770/R128 does not mandate an LRA target.  FFmpeg's loudnorm filter
# requires one, so use its maximum to avoid imposing unrequested compression.
ITU_LOUDNESS_RANGE_LU = 50
ITU_TRUE_PEAK_LINEAR = 0.891251
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_log = logging.getLogger(__name__)


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
    if bool(getattr(cfg, "metadata_suppressed", False)):
        return
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
    stream_album = _normalize_metadata_value(
        clean_album_metadata(getattr(cfg, "stream_album", ""))
    )
    if stream_title:
        cmd.extend(["-metadata", f"title={stream_title}"])
    if stream_artist:
        cmd.extend(["-metadata", f"artist={stream_artist}"])
    if stream_album:
        cmd.extend(["-metadata", f"album={stream_album}"])


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


def _broadcast_processing_filters(cfg: StationPipelineConfig) -> list[str]:
    """Return one standards-based processing chain for every station.

    ITU-R BS.1770 is programme-neutral: it specifies K-weighted, gated
    loudness and true-peak measurement, not genre EQ or compressor ratios.
    EBU R128 adds the -23 LUFS operational target and -1 dBTP ceiling.  Keep
    crossfades and codecs elsewhere in the pipeline; they are outside the
    standard and must not alter measurement policy.
    """

    loudness_target = getattr(cfg, "loudness_target_lufs", None)
    target = (
        ITU_PROGRAM_LOUDNESS_LUFS
        if loudness_target is None
        else max(-24.0, min(-9.0, float(loudness_target)))
    )
    return [
        (
            f"loudnorm=I={target:.1f}:TP={ITU_TRUE_PEAK_DBTP:.1f}:"
            f"LRA={ITU_LOUDNESS_RANGE_LU}"
        ),
        (
            f"alimiter=limit={ITU_TRUE_PEAK_LINEAR:.6f}:attack=5:release=80:"
            "level=false:latency=true"
        ),
    ]


def _icecast_filter_chain(cfg: StationPipelineConfig) -> list[str]:
    profile = resolve_stream_profile(cfg.stream_codec_profile, cfg.stream_bitrate_kbps)
    filters: list[str] = []
    final_resample_filters: list[str] = []
    profile_filters = [str(item) for item in profile.get("ffmpeg_filter_args", [])]
    for index, item in enumerate(profile_filters):
        if item == "-af" and index + 1 < len(profile_filters):
            profile_filter = profile_filters[index + 1]
            if profile_filter.startswith("aresample="):
                final_resample_filters.append(profile_filter)
            else:
                filters.append(profile_filter)
    processing_filters = _broadcast_processing_filters(cfg)
    limiter_filters = [
        item for item in processing_filters if item.startswith("alimiter=")
    ]
    filters.extend(
        item for item in processing_filters if not item.startswith("alimiter=")
    )
    if abs(float(cfg.output_gain_db or 0.0)) > 0.001:
        filters.append(f"volume={float(cfg.output_gain_db):.2f}dB")
    # Station gain must feed the safety limiter, never follow it.  A final
    # explicit resample returns loudnorm's internal rate to the encoder rate.
    filters.extend(limiter_filters)
    filters.extend(final_resample_filters or [f"aresample={LOCAL_PCM_SAMPLE_RATE}"])
    return filters


def _icecast_output_args(
    cfg: StationPipelineConfig,
    *,
    include_audio_filters: bool = True,
) -> list[str]:
    profile = resolve_stream_profile(cfg.stream_codec_profile, cfg.stream_bitrate_kbps)
    args: list[str] = []
    if include_audio_filters:
        filters = _icecast_filter_chain(cfg)
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
_FAST_AUDIO_CACHE_MAX_BYTES = 4 * 1024 * 1024 * 1024
_FAST_AUDIO_CACHE_IN_FLIGHT: set[str] = set()
_FAST_AUDIO_CACHE_LOCK = threading.Lock()
_FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT = False
_FAST_AUDIO_CACHE_PRUNE_LOCK = threading.Lock()


def _fast_cache_max_bytes() -> int:
    raw = str(os.getenv("CLEANROOM_FAST_AUDIO_CACHE_MAX_BYTES", "") or "").strip()
    if raw:
        try:
            return max(256 * 1024 * 1024, int(raw))
        except (TypeError, ValueError):
            pass
    return int(_FAST_AUDIO_CACHE_MAX_BYTES)


def _fast_cache_key(input_uri: str) -> str:
    """Return a stable key that changes when the source file changes."""

    try:
        stat = os.stat(input_uri)
        material = f"{os.path.abspath(input_uri)}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        material = os.path.abspath(input_uri)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _fast_cached_path(input_uri: str) -> str:
    filename = os.path.basename(input_uri)
    return os.path.join(
        FAST_AUDIO_CACHE_DIR,
        f"{_fast_cache_key(input_uri)}_{filename}",
    )


def prune_fast_audio_cache(
    *,
    max_bytes: int | None = None,
    min_age_seconds: float = 300.0,
    max_deletions: int = 512,
) -> dict:
    """Bound the disposable read-ahead cache without touching recent media.

    This function is safe to run while broadcasting: recent cache entries are
    protected, open files simply fail deletion on Windows, and every failure is
    counted instead of interrupting playout.
    """

    root = os.path.abspath(FAST_AUDIO_CACHE_DIR)
    target_bytes = _fast_cache_max_bytes() if max_bytes is None else max(0, int(max_bytes))
    protected_since = time.time() - max(0.0, float(min_age_seconds))
    limit = max(0, int(max_deletions))
    try:
        os.makedirs(root, exist_ok=True)
        entries: list[tuple[float, str, int]] = []
        total_bytes = 0
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if name.endswith(".part") or not os.path.isfile(path):
                continue
            try:
                stat_result = os.stat(path)
            except OSError:
                continue
            total_bytes += int(stat_result.st_size)
            entries.append(
                (float(stat_result.st_mtime), path, int(stat_result.st_size))
            )
    except OSError:
        return {
            "ok": False,
            "before_bytes": 0,
            "after_bytes": 0,
            "deleted_files": 0,
            "failed_files": 0,
        }

    before_bytes = total_bytes
    deleted_files = 0
    failed_files = 0
    for modified_at, path, size in sorted(entries, key=lambda item: (item[0], item[1])):
        if total_bytes <= target_bytes or deleted_files >= limit:
            break
        if modified_at >= protected_since:
            continue
        try:
            try:
                os.remove(path)
            except PermissionError:
                # copy2 can inherit a read-only source attribute on Windows.
                # Cache files are disposable; source-library attributes are
                # never changed.
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                os.remove(path)
            total_bytes = max(0, total_bytes - size)
            deleted_files += 1
        except OSError:
            failed_files += 1
    return {
        "ok": True,
        "before_bytes": before_bytes,
        "after_bytes": total_bytes,
        "deleted_files": deleted_files,
        "failed_files": failed_files,
        "target_bytes": target_bytes,
    }


def request_fast_audio_cache_prune() -> None:
    """Prune in a daemon so a track handoff never waits on directory cleanup."""

    global _FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT
    with _FAST_AUDIO_CACHE_PRUNE_LOCK:
        if _FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT:
            return
        _FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT = True

    def _worker() -> None:
        global _FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT
        try:
            # Multiple bounded passes can recover a legacy oversized cache
            # without monopolizing the audio worker that requested cleanup.
            for _ in range(64):
                result = prune_fast_audio_cache(max_deletions=256)
                if (
                    not result.get("ok")
                    or int(result.get("after_bytes", 0)) <= int(result.get("target_bytes", 0))
                    or int(result.get("deleted_files", 0)) == 0
                ):
                    break
                time.sleep(0.05)
        finally:
            with _FAST_AUDIO_CACHE_PRUNE_LOCK:
                _FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT = False

    threading.Thread(
        target=_worker,
        name="radiotedu-fast-audio-cache-prune",
        daemon=True,
    ).start()


def release_fast_cached_uri(input_uri: str, *, delay_seconds: float = 2.0) -> bool:
    """Delete one consumed cache entry if it has not been reused meanwhile."""

    if not input_uri or not os.path.isfile(input_uri):
        return False
    drive, _ = os.path.splitdrive(input_uri)
    if drive and drive.upper() == "C:":
        return False
    cached_path = _fast_cached_path(input_uri)
    try:
        expected = os.stat(cached_path)
    except OSError:
        return False

    expected_fingerprint = (int(expected.st_size), int(expected.st_mtime_ns))

    def _worker() -> None:
        if delay_seconds > 0:
            time.sleep(float(delay_seconds))
        try:
            current = os.stat(cached_path)
            current_fingerprint = (int(current.st_size), int(current.st_mtime_ns))
            # A cache hit updates mtime.  Never delete an entry another station
            # or a future queue item reused after this release was scheduled.
            if current_fingerprint != expected_fingerprint:
                return
            os.remove(cached_path)
        except OSError:
            request_fast_audio_cache_prune()

    threading.Thread(
        target=_worker,
        name="radiotedu-fast-audio-cache-release",
        daemon=True,
    ).start()
    return True


def _resolve_fast_cached_uri(input_uri: str) -> str:
    if not input_uri or not os.path.exists(input_uri):
        return input_uri
    drive, _ = os.path.splitdrive(input_uri)
    if drive and drive.upper() == "C:":
        return input_uri
    try:
        os.makedirs(FAST_AUDIO_CACHE_DIR, exist_ok=True)
        cached_path = _fast_cached_path(input_uri)
        if os.path.exists(cached_path) and os.path.getsize(cached_path) == os.path.getsize(input_uri):
            try:
                os.utime(cached_path, None)
            except OSError:
                pass
            request_fast_audio_cache_prune()
            return cached_path
        # Copy to a sidecar and atomically publish it. A background prefetch
        # must never expose a half-written file to the next FFmpeg process.
        partial_path = f"{cached_path}.{os.getpid()}.part"
        shutil.copy2(input_uri, partial_path)
        os.replace(partial_path, cached_path)
        try:
            os.chmod(cached_path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        # copy2 preserves the library file's old timestamp.  Cache mtime must
        # represent access time or LRU cleanup will evict a brand-new copy.
        os.utime(cached_path, None)
        request_fast_audio_cache_prune()
        return cached_path
    except Exception:
        try:
            if "partial_path" in locals() and os.path.exists(partial_path):
                os.remove(partial_path)
        except OSError:
            pass
        _log.debug("Fast audio cache fallback for %s", input_uri, exc_info=True)
        return input_uri


def prefetch_fast_cached_uri(input_uri: str) -> None:
    """Warm a slow-volume track on C: without delaying the playout worker.

    Queue filling happens tens of seconds before the handoff.  Copying the
    next H:/network-volume file synchronously at transition time was the
    source of long silent gaps.  The copy is atomic and de-duplicated per
    process; failures simply leave the normal source path in use.
    """

    if not input_uri or not os.path.isfile(input_uri):
        return
    drive, _ = os.path.splitdrive(input_uri)
    if drive and drive.upper() == "C:":
        return
    key = os.path.abspath(input_uri)
    with _FAST_AUDIO_CACHE_LOCK:
        if key in _FAST_AUDIO_CACHE_IN_FLIGHT:
            return
        _FAST_AUDIO_CACHE_IN_FLIGHT.add(key)

    def _worker() -> None:
        try:
            _resolve_fast_cached_uri(input_uri)
        finally:
            with _FAST_AUDIO_CACHE_LOCK:
                _FAST_AUDIO_CACHE_IN_FLIGHT.discard(key)

    threading.Thread(
        target=_worker,
        name="radiotedu-fast-audio-prefetch",
        daemon=True,
    ).start()


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
        f"afade=t=out:st=0:d={seconds}:curve=qsin[current_xf];"
        f"[1:a]asplit=2[next_head][next_tail];"
        f"[next_head]atrim=0:{seconds},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={seconds}:curve=qsin[next_xf];"
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
    icecast_map = "[outa]"
    local_map = "[outa]"
    if next_cfg.icecast_enabled:
        filter_index = cmd.index("-filter_complex") + 1
        filter_chain = ",".join(_icecast_filter_chain(next_cfg)) or "anull"
        if include_local_pipe:
            cmd[filter_index] += (
                ";[outa]asplit=2[icecast_input][local_out];"
                f"[icecast_input]{filter_chain}[icecast_out]"
            )
            local_map = "[local_out]"
        else:
            cmd[filter_index] += f";[outa]{filter_chain}[icecast_out]"
        icecast_map = "[icecast_out]"
    if next_cfg.icecast_enabled:
        cmd.extend(
            [
                "-map",
                icecast_map,
                *_icecast_output_args(next_cfg, include_audio_filters=False),
            ]
        )
        _append_track_metadata(cmd, next_cfg)
        cmd.append(_icecast_output_url(next_cfg))
        wrote_output = True
    if include_local_pipe:
        cmd.extend(
            [
                "-map",
                local_map,
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
