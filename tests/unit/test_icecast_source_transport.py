from __future__ import annotations

import base64

import pytest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_source_transport import (
    IcecastSourceProtocolError,
    IcecastSourceTransport,
)


class FakeSocket:
    def __init__(self, response=b"HTTP/1.1 200 OK\r\n\r\n"):
        self.response = response
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, payload):
        self.sent.append(bytes(payload))

    def recv(self, _size):
        response, self.response = self.response, b""
        return response

    def shutdown(self, _how):
        return None

    def close(self):
        self.closed = True


def _config(
    profile: str = "aac_low_192",
    bitrate_kbps: int = 192,
    mount: str = "/lofi",
):
    return StationPipelineConfig(
        input_uri="virtual:silence",
        icecast_host="127.0.0.1",
        icecast_port=11154,
        icecast_mount=mount,
        icecast_user="source",
        icecast_password="private-source-secret",
        local_output_enabled=False,
        output_device_id="",
        stream_codec_profile=profile,
        stream_bitrate_kbps=bitrate_kbps,
        icecast_stream_name="RadioTEDU Lo-Fi\r\nInjected: no",
        icecast_description="RadioTEDU",
        icecast_genre="Lo-Fi",
    )


def test_source_authentication_stays_in_socket_header_not_process_command():
    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config(), socket_factory=lambda *_args, **_kwargs: source_socket
    )
    try:
        assert source_socket.timeout == 5.0
        handshake = source_socket.sent[0]
        expected = base64.b64encode(
            b"source:private-source-secret"
        )
        assert handshake.startswith(b"PUT /lofi HTTP/1.1\r\n")
        assert b"Authorization: Basic " + expected in handshake
        assert b"\r\nInjected:" not in handshake
        transport.send(b"encoded-audio")
        assert source_socket.sent[-1] == b"encoded-audio"
    finally:
        transport.close()
    assert source_socket.closed


def test_source_rejection_has_no_credential_echo():
    source_socket = FakeSocket(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
    with pytest.raises(IcecastSourceProtocolError) as exc:
        IcecastSourceTransport(
            _config(), socket_factory=lambda *_args, **_kwargs: source_socket
        )
    assert "private-source-secret" not in str(exc.value)


@pytest.mark.parametrize(
    ("profile", "bitrate_kbps"),
    (("aac_low_192", 192), ("aac_he_v2_64", 64)),
)
def test_lossy_aac_source_advertises_real_icecast_bitrate(profile, bitrate_kbps):
    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config(profile, bitrate_kbps),
        socket_factory=lambda *_args, **_kwargs: source_socket,
    )
    try:
        handshake = source_socket.sent[0]
        assert f"Ice-Bitrate: {bitrate_kbps}\r\n".encode() in handshake
        assert (
            f"Ice-Audio-Info: ice-bitrate={bitrate_kbps};"
            "ice-samplerate=48000;ice-channels=2\r\n"
        ).encode() in handshake
    finally:
        transport.close()


def test_flac_source_handshake_is_unchanged_by_bitrate_metadata_policy():
    source_socket = FakeSocket()
    transport = IcecastSourceTransport(
        _config("ogg_flac_lossless", 0, "/classic-flac"),
        socket_factory=lambda *_args, **_kwargs: source_socket,
    )
    try:
        handshake = source_socket.sent[0]
        assert b"Ice-Bitrate:" not in handshake
        assert b"Ice-Audio-Info:" not in handshake
    finally:
        transport.close()
