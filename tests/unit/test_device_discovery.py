from app.audio.device_discovery import (
    parse_ffmpeg_dshow_audio_devices,
    parse_ffmpeg_dshow_input_devices,
)


def test_parse_ffmpeg_dshow_audio_devices():
    sample = """
[dshow @ 000001f68b9f6bc0] "Integrated Webcam"
[dshow @ 000001f68b9f6bc0]   Alternative name "@device_pnp_\\\\?\\usb#vid_0bda"
[dshow @ 000001f68b9f6bc0] "Speakers (USB Audio)"
[dshow @ 000001f68b9f6bc0]   Alternative name "@device_cm_{{33D9A762-90C8-11D0-BD43-00A0C911CE86}}\\\\wave_{123}"
"""
    devices = parse_ffmpeg_dshow_audio_devices(sample)
    assert "Integrated Webcam" in devices
    assert "Speakers (USB Audio)" in devices


def test_parse_ffmpeg_dshow_input_devices_only_returns_audio_input_labels():
    sample = """
[dshow @ 000001] DirectShow video devices
[dshow @ 000001]  "Integrated Webcam"
[dshow @ 000001] DirectShow audio devices
[dshow @ 000001]  "Studio Microphone (USB Audio)"
[dshow @ 000001]    Alternative name "@device_cm_{hardware-id}\\\\wave"
"""

    assert parse_ffmpeg_dshow_input_devices(sample) == ["Studio Microphone (USB Audio)"]
