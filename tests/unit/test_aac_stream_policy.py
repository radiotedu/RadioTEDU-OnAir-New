from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_audio_sink import current_codec_fallback


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
    assert fallback.stream_codec_profile == "he_aac_192"
    assert fallback.stream_bitrate_kbps == 192


def test_new_low_profile_falls_back_to_current_low_profile() -> None:
    fallback = current_codec_fallback(_cfg("aac_he_v2_64", 64))

    assert fallback is not None
    assert fallback.stream_codec_profile == "he_aac_96"
    assert fallback.stream_bitrate_kbps == 96


def test_flac_never_receives_an_aac_fallback() -> None:
    assert current_codec_fallback(_cfg("ogg_flac_lossless", 0)) is None
