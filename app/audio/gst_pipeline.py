from dataclasses import dataclass
from math import pow

from app.audio.virtual_sources import is_silence_input_uri


@dataclass(slots=True)
class StationPipelineConfig:
    input_uri: str
    icecast_host: str
    icecast_port: int
    icecast_mount: str
    icecast_user: str
    icecast_password: str
    local_output_enabled: bool
    output_device_id: str
    output_gain_db: float = 0.0
    loudness_target_lufs: float | None = None
    # "balanced" mirrors a conservative professional web-radio chain;
    # "transparent" keeps wide dynamics for Classical/Jazz; "off" retains
    # only codec-required filtering and the configured loudness stage.
    broadcast_processing_profile: str = "balanced"
    stream_codec_profile: str = "aac_low_192"
    stream_bitrate_kbps: int = 192
    icecast_enabled: bool = True
    stream_title: str = ""
    stream_artist: str = ""
    # Icecast exposes one public now-playing string.  Keep album separate in
    # the runtime config so it can be composed without changing the program
    # title/artist fields used by the UI and playout state.
    stream_album: str = ""
    track_type: str = "music"
    crossfade_seconds: float = 0.0
    station_name: str = ""
    icecast_stream_name: str = ""
    icecast_description: str = ""
    icecast_genre: str = ""
    icecast_url: str = ""
    icecast_public: bool = True
    icecast_user_agent: str = "RadioTEDU OnAir"
    icecast_tls_enabled: bool = False
    icecast_legacy_source_enabled: bool = False
    source_protocol: str = "icecast"
    extra_icecast_outputs: tuple[dict, ...] = ()
    # Some stations intentionally expose no now-playing metadata.  This is a
    # station/output policy and must not disable the audio encoder itself.
    metadata_suppressed: bool = False


def _db_to_linear(db: float) -> float:
    return pow(10.0, float(db) / 20.0)


def _q(value: str) -> str:
    return value.replace('"', '\\"')


def _profile_bitrate(
    token: str,
    configured_bitrate_kbps: int,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    suffix = token.rsplit("_", 1)[-1]
    try:
        candidate = int(suffix) if suffix.isdigit() else int(configured_bitrate_kbps)
    except (TypeError, ValueError):
        candidate = default
    return max(minimum, min(maximum, candidate))


def resolve_stream_profile(
    profile: str, bitrate_kbps: int
) -> dict[str, str | int | list[str] | bool]:
    token = str(profile or "").strip().lower().replace("-", "_")

    if token.startswith(("ogg_flac", "flac_ogg")) or token in {"oggflac", "flacogg"}:
        return {
            "profile": "ogg_flac_lossless",
            "codec": "flac",
            "bitrate_kbps": 0,
            "ffmpeg_codec": "flac",
            "ffmpeg_encoder_args": ["-compression_level", "8"],
            "content_type": "application/ogg",
            "format": "ogg",
            "gst_encoder": "flacenc ! oggmux",
            "uses_bitrate": False,
        }

    if token.startswith("mp3"):
        bitrate = _profile_bitrate(token, bitrate_kbps, 128, 96, 320)
        return {
            "profile": f"mp3_{bitrate}",
            "codec": "mp3",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "libmp3lame",
            "content_type": "audio/mpeg",
            "format": "mp3",
            "gst_encoder": f"lamemp3enc target=1 bitrate={bitrate} cbr=true",
            "uses_bitrate": True,
        }

    if token.startswith("opus"):
        bitrate = _profile_bitrate(token, bitrate_kbps, 128, 32, 320)
        return {
            "profile": f"opus_{bitrate}",
            "codec": "opus",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "libopus",
            "ffmpeg_filter_args": ["-af", "aresample=48000"],
            "ffmpeg_encoder_args": [
                "-vbr",
                "constrained",
                "-application",
                "audio",
                "-compression_level",
                "10",
                "-frame_duration",
                "20",
            ],
            "content_type": "audio/ogg",
            "format": "ogg",
            "gst_encoder": f"opusenc bitrate={bitrate * 1000} ! oggmux",
            "uses_bitrate": True,
        }

    if token.startswith(("aac_he_v2", "he_aac_v2")):
        bitrate = _profile_bitrate(token, bitrate_kbps, 64, 24, 128)
        return {
            "profile": f"aac_he_v2_{bitrate}",
            "codec": "aac",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "libfdk_aac",
            "ffmpeg_profile": "aac_he_v2",
            "ffmpeg_filter_args": ["-af", "aresample=48000"],
            "ffmpeg_encoder_args": ["-afterburner", "1"],
            "content_type": "audio/aac",
            "format": "adts",
            "gst_encoder": (
                f"fdkaacenc bitrate={bitrate * 1000} afterburner=true "
                "! audio/mpeg,mpegversion=4,stream-format=adts,profile=he-aac-v2"
            ),
            "uses_bitrate": True,
        }

    if token.startswith("aac_low"):
        bitrate = _profile_bitrate(token, bitrate_kbps, 192, 32, 512)
        return {
            "profile": f"aac_low_{bitrate}",
            "codec": "aac",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "libfdk_aac",
            "ffmpeg_profile": "aac_low",
            "ffmpeg_filter_args": ["-af", "aresample=48000"],
            "ffmpeg_encoder_args": ["-afterburner", "1"],
            "content_type": "audio/aac",
            "format": "adts",
            "gst_encoder": (
                f"fdkaacenc bitrate={bitrate * 1000} afterburner=true "
                "! audio/mpeg,mpegversion=4,stream-format=adts,profile=lc"
            ),
            "uses_bitrate": True,
        }

    if token.startswith("he_aac"):
        bitrate = _profile_bitrate(token, bitrate_kbps, 128, 32, 320)
        return {
            "profile": f"he_aac_{bitrate}",
            "codec": "aac",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "libfdk_aac",
            "ffmpeg_profile": "aac_he",
            # FDK-AAC HE-AAC v1 is kept at the broadcast sample rate.  The
            # final Icecast branch adds the shared loudness filter afterwards.
            "ffmpeg_filter_args": ["-af", "aresample=48000"],
            "ffmpeg_encoder_args": ["-afterburner", "1"],
            "content_type": "audio/aac",
            "format": "adts",
            "gst_encoder": (
                f"fdkaacenc bitrate={bitrate * 1000} afterburner=true "
                "! audio/mpeg,mpegversion=4,stream-format=adts,profile=he-aac-v1"
            ),
            "uses_bitrate": True,
        }

    if token.startswith(("aac_lc", "aac")):
        bitrate = _profile_bitrate(token, bitrate_kbps, 128, 32, 512)
        return {
            "profile": f"aac_lc_{bitrate}",
            "codec": "aac",
            "bitrate_kbps": bitrate,
            "ffmpeg_codec": "aac",
            "ffmpeg_profile": "aac_low",
            "content_type": "audio/aac",
            "format": "adts",
            "gst_encoder": f"voaacenc bitrate={max(64000, min(320000, bitrate * 1000))}",
            "uses_bitrate": True,
        }

    bitrate = _profile_bitrate(token, bitrate_kbps, 192, 32, 512)
    return {
        "profile": f"aac_low_{bitrate}",
        "codec": "aac",
        "bitrate_kbps": bitrate if bitrate > 0 else 192,
        "ffmpeg_codec": "libfdk_aac",
        "ffmpeg_profile": "aac_low",
        "ffmpeg_filter_args": ["-af", "aresample=48000"],
        "ffmpeg_encoder_args": ["-afterburner", "1"],
        "content_type": "audio/aac",
        "format": "adts",
        "gst_encoder": (
            f"fdkaacenc bitrate={(bitrate if bitrate > 0 else 192) * 1000} afterburner=true "
            "! audio/mpeg,mpegversion=4,stream-format=adts,profile=lc"
        ),
        "uses_bitrate": True,
    }


def build_gst_pipeline(cfg: StationPipelineConfig) -> str:
    if is_silence_input_uri(cfg.input_uri):
        source = (
            "audiotestsrc wave=silence is-live=true "
            "! audio/x-raw,rate=48000,channels=2 "
            "! audioconvert ! audioresample ! tee name=t"
        )
    else:
        input_location = _q(cfg.input_uri)
        source = (
            f'filesrc location="{input_location}" ! decodebin ! audioconvert ! audioresample ! tee name=t'
        )
    branches = []
    if cfg.icecast_enabled:
        profile = resolve_stream_profile(cfg.stream_codec_profile, cfg.stream_bitrate_kbps)
        mount = (
            cfg.icecast_mount
            if cfg.icecast_mount.startswith("/")
            else f"/{cfg.icecast_mount}"
        )
        icecast = (
            f"t. ! queue ! audioconvert ! {profile['gst_encoder']} "
            f"! shout2send ip={cfg.icecast_host} port={int(cfg.icecast_port)} mount={mount} "
            f'username="{_q(cfg.icecast_user)}" password="{_q(cfg.icecast_password)}"'
        )
        branches.append(icecast)
    if cfg.local_output_enabled:
        local = (
            "t. ! queue ! audioconvert ! audioresample "
            f"! volume volume={_db_to_linear(cfg.output_gain_db):.6f} "
        )
        if cfg.output_device_id:
            local += f'! wasapisink device="{_q(cfg.output_device_id)}"'
        else:
            local += "! wasapisink"
        branches.append(local)
    if not branches:
        raise ValueError("at least one output target must be enabled")
    return " ".join([source, *branches])
