from __future__ import annotations

import base64
import select
import socket
import ssl
from typing import Callable

from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile


DEFAULT_SOURCE_WRITE_TIMEOUT_SECONDS = 5.0


class IcecastSourceProtocolError(RuntimeError):
    """A credential-safe source connection failure."""


def _header(value: object, maximum: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return "".join(character for character in text if character.isprintable())[:maximum]


class IcecastSourceTransport:
    """Authenticated Icecast/TinyIce PUT transport with no process-list secrets."""

    def __init__(
        self,
        cfg: StationPipelineConfig,
        *,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        connect_timeout_sec: float = 5.0,
        handshake_timeout_sec: float = 10.0,
        write_timeout_sec: float | None = DEFAULT_SOURCE_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        host = _header(cfg.icecast_host, 255)
        port = int(cfg.icecast_port or 0)
        mount = _header(cfg.icecast_mount, 512)
        if not host or not 1 <= port <= 65535 or not mount:
            raise IcecastSourceProtocolError("Icecast source destination is invalid")
        if not mount.startswith("/"):
            mount = f"/{mount}"
        user = _header(cfg.icecast_user, 80)
        password = str(cfg.icecast_password or "")
        if not user or not password:
            raise IcecastSourceProtocolError("Icecast source credential is not configured")

        source_socket = None
        try:
            source_socket = socket_factory(
                (host, port), timeout=max(0.1, float(connect_timeout_sec))
            )
            try:
                source_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except (AttributeError, OSError):
                pass
            for option_name, value in (
                ("TCP_KEEPIDLE", 10),
                ("TCP_KEEPINTVL", 3),
                ("TCP_KEEPCNT", 3),
            ):
                option = getattr(socket, option_name, None)
                if option is not None:
                    try:
                        source_socket.setsockopt(socket.IPPROTO_TCP, option, value)
                    except (AttributeError, OSError):
                        pass
            if bool(getattr(cfg, "icecast_tls_enabled", False)):
                source_socket = ssl.create_default_context().wrap_socket(
                    source_socket, server_hostname=host
                )
            source_socket.settimeout(max(0.1, float(handshake_timeout_sec)))
            profile = resolve_stream_profile(
                cfg.stream_codec_profile, cfg.stream_bitrate_kbps
            )
            authorization = base64.b64encode(
                f"{user}:{password}".encode("utf-8")
            ).decode("ascii")
            method = (
                "SOURCE"
                if bool(getattr(cfg, "icecast_legacy_source_enabled", False))
                else "PUT"
            )
            bitrate_headers = ""
            if bool(profile.get("uses_bitrate")):
                bitrate = int(profile.get("bitrate_kbps") or 0)
                if bitrate > 0:
                    bitrate_headers = (
                        f"Ice-Bitrate: {bitrate}\r\n"
                        f"Ice-Audio-Info: ice-bitrate={bitrate};"
                        "ice-samplerate=48000;ice-channels=2\r\n"
                    )
            request = (
                f"{method} {mount} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Authorization: Basic {authorization}\r\n"
                f"User-Agent: {_header(getattr(cfg, 'icecast_user_agent', ''), 160) or 'RadioTEDU-OnAir-Source/1.0'}\r\n"
                f"Content-Type: {_header(profile.get('content_type'), 80)}\r\n"
                f"Ice-Name: {_header(getattr(cfg, 'icecast_stream_name', '') or getattr(cfg, 'station_name', ''))}\r\n"
                f"Ice-Description: {_header(getattr(cfg, 'icecast_description', ''))}\r\n"
                f"Ice-Genre: {_header(getattr(cfg, 'icecast_genre', ''))}\r\n"
                f"Ice-URL: {_header(getattr(cfg, 'icecast_url', ''))}\r\n"
                f"Ice-Public: {1 if bool(getattr(cfg, 'icecast_public', True)) else 0}\r\n"
                f"{bitrate_headers}"
                # A source PUT is the long-lived stream itself. Advertising
                # close lets small Icecast-compatible origins retire a mount
                # while the client still has buffered socket writes.
                "Connection: keep-alive\r\n\r\n"
            ).encode("utf-8")
            source_socket.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 16_384:
                chunk = source_socket.recv(2048)
                if not chunk:
                    break
                response.extend(chunk)
            if b"\r\n\r\n" not in response:
                raise IcecastSourceProtocolError(
                    "Icecast source handshake returned no complete HTTP response"
                )
            status = bytes(response).split(b"\r\n", 1)[0].decode(
                "ascii", errors="replace"
            )
            if " 200 " not in f" {status} ":
                raise IcecastSourceProtocolError(
                    f"Icecast rejected source: {status[:120]}"
                )
            source_socket.settimeout(
                None
                if write_timeout_sec is None
                else max(0.1, float(write_timeout_sec))
            )
            self._socket = source_socket
            self.content_type = str(profile.get("content_type") or "")
        except Exception:
            if source_socket is not None:
                try:
                    source_socket.close()
                except OSError:
                    pass
            raise

    def send(self, payload: bytes) -> None:
        if payload:
            self._socket.sendall(payload)

    def peer_closed(self) -> bool:
        """Detect a graceful/half-open peer close without consuming protocol data."""

        source_socket = getattr(self, "_socket", None)
        if source_socket is None:
            return True
        try:
            readable, _, exceptional = select.select(
                [source_socket], [], [source_socket], 0
            )
            if exceptional:
                return True
            if not readable:
                return False
            return source_socket.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError, TimeoutError):
            return False
        except OSError:
            return True

    def close(self) -> None:
        source_socket = getattr(self, "_socket", None)
        self._socket = None
        if source_socket is None:
            return
        try:
            source_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            source_socket.close()
        except OSError:
            pass
