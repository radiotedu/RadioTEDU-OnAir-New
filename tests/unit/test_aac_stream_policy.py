from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_audio_sink import (
    _codec_fallback_action,
    current_codec_fallback,
)


def _cfg(profile: str, bitrate_kbps: int) -> StationPipelineConfig:
    return StationPipelineConfig(
        input_uri="silence://continuous",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/test",
        icecast_user="source",
        icecast_password="secret",
        local_output_enabled=False,
        output_device_id="",
        stream_codec_profile=profile,
        stream_bitrate_kbps=bitrate_kbps,
    )


def test_new_normal_profile_falls_back_to_current_normal_profile() -> None:
    fallback = current_codec_fallback(_cfg("aac_low_192", 192))

    assert fallback is not None
    assert fallback.stream_codec_profile == "aac_lc_192"
    assert fallback.stream_bitrate_kbps == 192


def test_he_aac_v2_does_not_silently_change_to_another_profile() -> None:
    assert current_codec_fallback(_cfg("aac_he_v2_64", 64)) is None


def test_flac_never_receives_an_aac_fallback() -> None:
    assert current_codec_fallback(_cfg("ogg_flac_lossless", 0)) is None


def test_network_or_auth_failures_never_change_the_selected_encoder() -> None:
    assert _codec_fallback_action("Connection refused by Icecast") is None
    assert _codec_fallback_action("HTTP 401 Unauthorized") is None
    assert _codec_fallback_action("Connection timed out") is None


def test_unsupported_afterburner_only_retries_without_that_optional_option() -> None:
    error = "Unrecognized option 'afterburner'. Error splitting the argument list"

    assert _codec_fallback_action(error) == "omit_afterburner"


def test_native_aac_fallback_requires_explicit_missing_fdk_encoder() -> None:
    assert (
        _codec_fallback_action("Unknown encoder 'libfdk_aac'")
        == "native_aac"
    )
    assert _codec_fallback_action("Error initializing output stream") is None
