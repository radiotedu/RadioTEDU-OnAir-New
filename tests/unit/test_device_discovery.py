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


def test_modern_dshow_type_annotations_find_only_audio_inputs():
    sample = '\n'.join([
        '[dshow @ 0001] "Webcam" (video)',
        '[dshow @ 0001] "USB Broadcast Mic" (audio)',
        '[dshow @ 0001]   Alternative name "@device_hidden"',
        '[dshow @ 0001] "USB Broadcast Mic" (audio)',
    ])
    assert parse_ffmpeg_dshow_input_devices(sample) == ['USB Broadcast Mic']


def test_posix_discovery_never_invokes_directshow(monkeypatch):
    from app.audio import device_discovery
    monkeypatch.setattr(device_discovery.sys, 'platform', 'linux')
    monkeypatch.setattr(device_discovery.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('Windows probe invoked')))
    assert device_discovery.list_output_devices() == ['default']
    assert device_discovery.list_input_devices() == []
