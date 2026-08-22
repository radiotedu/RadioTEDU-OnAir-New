from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services import encoder_capabilities


def test_aac_capability_accepts_native_encoder(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"probe")
    encoder_capabilities._inspect_ffmpeg.cache_clear()

    with (
        patch.object(encoder_capabilities, "resolve_binary", return_value=str(ffmpeg)),
        patch.object(
            encoder_capabilities.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=" A....D aac AAC\n A....D libfdk_aac Fraunhofer FDK AAC\n",
            ),
        ),
    ):
        result = encoder_capabilities.inspect_aac_encoder()

    assert result["available"] is True
    assert result["encoder"] == "aac"
    assert result["profile"] == "AAC-LC"


def test_missing_native_aac_is_reported(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"probe")
    encoder_capabilities._inspect_ffmpeg.cache_clear()

    with (
        patch.object(encoder_capabilities, "resolve_binary", return_value=str(ffmpeg)),
        patch.object(
            encoder_capabilities.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=" A....D libmp3lame MP3\n"),
        ),
    ):
        result = encoder_capabilities.inspect_aac_encoder()

    assert result["available"] is False
    assert result["error_code"] == "aac_encoder_unavailable"


def test_opus_capability_requires_libopus(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"probe")
    encoder_capabilities._inspect_ffmpeg.cache_clear()

    with (
        patch.object(encoder_capabilities, "resolve_binary", return_value=str(ffmpeg)),
        patch.object(
            encoder_capabilities.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=" A....D opus Opus\n A....D libopus libopus Opus\n",
            ),
        ),
    ):
        result = encoder_capabilities.inspect_opus_encoder()

    assert result["available"] is True
    assert result["encoder"] == "libopus"
    assert result["profile"] == "Opus"


def test_fdk_capability_covers_normal_and_low_aac_profiles(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"probe")
    encoder_capabilities._inspect_ffmpeg.cache_clear()

    with (
        patch.object(encoder_capabilities, "resolve_binary", return_value=str(ffmpeg)),
        patch.object(
            encoder_capabilities.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=" A....D libfdk_aac Fraunhofer FDK AAC\n",
            ),
        ),
    ):
        result = encoder_capabilities.inspect_he_aac_encoder()

    assert result["available"] is True
    assert result["encoder"] == "libfdk_aac"
    assert result["profile"] == "AAC-LC / HE-AAC v2"
