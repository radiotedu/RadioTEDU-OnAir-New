from app.services import audio_stream_probe as probe


class _Response:
    def __init__(self, *, status=200, content_type="audio/aac", payload=b"\xff"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.payload[:size]


def test_probe_requires_audio_content_and_a_real_payload_byte(monkeypatch):
    monkeypatch.setattr(probe, "urlopen", lambda *_args, **_kwargs: _Response())
    result = probe.probe_audio_url("https://stream.example.test/lofi")
    assert result.ok is True
    assert result.sample_bytes == 1

    monkeypatch.setattr(
        probe,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload=b""),
    )
    result = probe.probe_audio_url("https://stream.example.test/lofi")
    assert result.ok is False
    assert result.reason == "empty_audio_payload"

    monkeypatch.setattr(
        probe,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            content_type="text/plain",
            payload=b"x",
        ),
    )
    result = probe.probe_audio_url("https://stream.example.test/lofi")
    assert result.ok is False
    assert result.reason == "non_audio_content"


def test_configured_listener_prefers_public_origin_and_normalizes_mount():
    output = {
        "icecast_host": "10.98.98.75",
        "icecast_port": 11154,
        "icecast_mount": "lofi",
    }
    assert probe.configured_listener_url(
        output,
        {"icecast_tls_enabled": "false"},
        "https://stream.radiotedu.com/",
    ) == "https://stream.radiotedu.com/lofi"


def test_configured_listener_uses_direct_tls_destination_without_public_origin():
    output = {
        "icecast_host": "stream.example.test",
        "icecast_port": 8443,
        "icecast_mount": "/safe mount",
    }
    assert probe.configured_listener_url(
        output,
        {"icecast_tls_enabled": "true"},
    ) == "https://stream.example.test:8443/safe%20mount"


def test_configured_shoutcast_listener_uses_port_below_legacy_source_port():
    output = {
        "source_protocol": "shoutcast",
        "icecast_host": "stream.example.test",
        "icecast_port": 8001,
        "icecast_mount": "",
    }

    assert probe.configured_listener_url(output) == "http://stream.example.test:8000/"
