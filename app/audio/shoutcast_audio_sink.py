from __future__ import annotations

import logging
import queue
import socket
import subprocess
import threading
import time
from typing import Callable

from app.audio.ffmpeg_pipeline import build_ffmpeg_encoded_sink_cmd
from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile

_log = logging.getLogger(__name__)

_PCM_QUEUE_MAX_CHUNKS = 64
_HANDSHAKE_RESPONSE_LIMIT = 4096
_ENCODED_CHUNK_BYTES = 64 * 1024


class ShoutcastProtocolError(RuntimeError):
    """A secret-safe source-protocol failure."""


def _header_value(value: object, maximum: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return "".join(character for character in text if character.isprintable())[:maximum]


def _source_credential(cfg: StationPipelineConfig) -> str:
    password = str(cfg.icecast_password or "")
    user = _header_value(cfg.icecast_user, 80)
    if user and user.lower() != "source":
        password = f"{user}:{password}"
    stream_id = max(1, int(getattr(cfg, "shoutcast_stream_id", 1) or 1))
    if stream_id > 1:
        password = f"{password}:#{stream_id}"
    return password


def _recv_handshake_response(source_socket, timeout_seconds: float) -> bytes:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    received = bytearray()
    while len(received) < _HANDSHAKE_RESPONSE_LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ShoutcastProtocolError("SHOUTcast source handshake timed out")
        source_socket.settimeout(remaining)
        try:
            chunk = source_socket.recv(
                min(512, _HANDSHAKE_RESPONSE_LIMIT - len(received))
            )
        except TimeoutError as exc:
            raise ShoutcastProtocolError(
                "SHOUTcast source handshake timed out"
            ) from exc
        if not chunk:
            break
        received.extend(chunk)
        normalized = bytes(received).replace(b"\r\n", b"\n")
        if b"\n\n" in normalized or normalized.startswith(b"OK2\n"):
            break
    return bytes(received)


def perform_shoutcast_v1_handshake(
    source_socket,
    cfg: StationPipelineConfig,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Authenticate a legacy DNAS source and send bounded ICY headers."""

    credential = _source_credential(cfg)
    if not credential:
        raise ShoutcastProtocolError("SHOUTcast source credential is not configured")
    source_socket.settimeout(max(0.1, float(timeout_seconds)))
    source_socket.sendall((credential + "\r\n").encode("utf-8"))
    response = _recv_handshake_response(source_socket, timeout_seconds)
    response_lines = {
        line.strip().upper()
        for line in response.replace(b"\r\n", b"\n").split(b"\n")
        if line.strip()
    }
    if b"OK2" not in response_lines:
        raise ShoutcastProtocolError("SHOUTcast source authentication was rejected")

    profile = resolve_stream_profile(
        cfg.stream_codec_profile,
        cfg.stream_bitrate_kbps,
    )
    codec = str(profile.get("codec") or "").lower()
    if codec not in {"mp3", "aac"}:
        raise ShoutcastProtocolError(
            "SHOUTcast legacy source supports only MP3 or AAC profiles"
        )
    content_type = "audio/mpeg" if codec == "mp3" else "audio/aac"
    headers = [
        ("icy-name", _header_value(cfg.icecast_stream_name or cfg.station_name)),
        ("icy-genre", _header_value(cfg.icecast_genre)),
        ("icy-url", _header_value(cfg.icecast_url)),
        ("icy-pub", "1" if bool(cfg.icecast_public) else "0"),
        ("icy-br", str(int(profile.get("bitrate_kbps") or 0))),
        ("content-type", content_type),
    ]
    payload = "".join(f"{name}:{value}\r\n" for name, value in headers)
    source_socket.sendall((payload + "\r\n").encode("utf-8"))


class ShoutcastAudioSink:
    protocol = "shoutcast"

    def __init__(
        self,
        ffmpeg_bin: str,
        spawn_process: Callable[..., subprocess.Popen],
        *,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
        connect_timeout_sec: float = 5.0,
        handshake_timeout_sec: float = 5.0,
        write_timeout_sec: float = 5.0,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self._spawn_process = spawn_process
        self._socket_factory = socket_factory
        self._connect_timeout_sec = max(0.1, float(connect_timeout_sec))
        self._handshake_timeout_sec = max(0.1, float(handshake_timeout_sec))
        self._write_timeout_sec = max(0.1, float(write_timeout_sec))
        self._process = None
        self._socket = None
        self._signature = None
        self._pcm_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=_PCM_QUEUE_MAX_CHUNKS
        )
        self._stop_event = threading.Event()
        self._writer_thread = None
        self._network_thread = None
        self._lock = threading.Lock()
        self._handshake_accepted = False
        self._network_failed = False
        self._writer_failed = False
        self._writer_backpressured = False
        self._dropped_pcm_chunks = 0
        self._encoded_bytes_sent = 0
        self._last_pcm_write_monotonic = None
        self._last_network_write_monotonic = None

    @property
    def process(self):
        return self._process

    @property
    def stdin(self):
        return getattr(self._process, "stdin", None) if self._process else None

    @staticmethod
    def _cfg_signature(cfg: StationPipelineConfig) -> tuple:
        return (
            str(cfg.icecast_host or "").strip(),
            int(cfg.icecast_port),
            str(cfg.icecast_user or "").strip(),
            str(cfg.icecast_password or ""),
            str(cfg.stream_codec_profile or ""),
            int(cfg.stream_bitrate_kbps or 0),
            int(getattr(cfg, "shoutcast_stream_id", 1) or 1),
        )

    def is_running(self) -> bool:
        with self._lock:
            return bool(
                self._process
                and self._process.poll() is None
                and self._handshake_accepted
                and not self._network_failed
            )

    def accepts_input(self) -> bool:
        return bool(self.is_running() and self.stdin is not None)

    def write_pcm(self, chunk: bytes) -> bool:
        if not chunk or not self.accepts_input():
            return False
        try:
            self._pcm_queue.put_nowait(bytes(chunk))
        except queue.Full:
            with self._lock:
                self._writer_backpressured = True
                self._dropped_pcm_chunks += 1
            return False
        return True

    def health_snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            pcm_age = (
                None
                if self._last_pcm_write_monotonic is None
                else max(0.0, now - self._last_pcm_write_monotonic)
            )
            network_age = (
                None
                if self._last_network_write_monotonic is None
                else max(0.0, now - self._last_network_write_monotonic)
            )
            connection_healthy = bool(
                self._handshake_accepted
                and not self._network_failed
                and self._encoded_bytes_sent > 0
            )
            return {
                "process_running": bool(
                    self._process and self._process.poll() is None
                ),
                "mount_healthy": connection_healthy,
                "connection_healthy": connection_healthy,
                "handshake_accepted": bool(self._handshake_accepted),
                "consecutive_probe_failures": int(self._network_failed),
                "writer_running": bool(
                    self._writer_thread and self._writer_thread.is_alive()
                ),
                "network_writer_running": bool(
                    self._network_thread and self._network_thread.is_alive()
                ),
                "writer_failed": bool(self._writer_failed),
                "network_failed": bool(self._network_failed),
                "writer_backpressured": bool(self._writer_backpressured),
                "queued_pcm_chunks": int(self._pcm_queue.qsize()),
                "dropped_pcm_chunks": int(self._dropped_pcm_chunks),
                "encoded_bytes_sent": int(self._encoded_bytes_sent),
                "last_write_age_seconds": (
                    None if pcm_age is None else round(pcm_age, 3)
                ),
                "last_network_write_age_seconds": (
                    None if network_age is None else round(network_age, 3)
                ),
                "source_protocol": self.protocol,
            }

    def _clear_queue(self) -> None:
        while True:
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                return

    def _fail_network(self) -> None:
        with self._lock:
            self._network_failed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    def _start_threads(self) -> None:
        self._stop_event.clear()
        self._clear_queue()

        def write_pcm() -> None:
            while not self._stop_event.is_set():
                try:
                    chunk = self._pcm_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                stdin = self.stdin
                if stdin is None or not self.is_running():
                    continue
                try:
                    stdin.write(chunk)
                    flush = getattr(stdin, "flush", None)
                    if callable(flush):
                        flush()
                    with self._lock:
                        self._writer_failed = False
                        self._writer_backpressured = False
                        self._last_pcm_write_monotonic = time.monotonic()
                except Exception:
                    with self._lock:
                        self._writer_failed = True
                    self._fail_network()
                    return

        def send_encoded() -> None:
            process = self._process
            encoded = getattr(process, "stdout", None) if process else None
            source_socket = self._socket
            if encoded is None or source_socket is None:
                self._fail_network()
                return
            while not self._stop_event.is_set():
                try:
                    chunk = encoded.read(_ENCODED_CHUNK_BYTES)
                    if not chunk:
                        self._fail_network()
                        return
                    source_socket.settimeout(self._write_timeout_sec)
                    source_socket.sendall(chunk)
                    with self._lock:
                        self._encoded_bytes_sent += len(chunk)
                        self._last_network_write_monotonic = time.monotonic()
                except (OSError, TimeoutError):
                    self._fail_network()
                    return

        self._writer_thread = threading.Thread(
            target=write_pcm,
            name="shoutcast-pcm-writer",
            daemon=True,
        )
        self._network_thread = threading.Thread(
            target=send_encoded,
            name="shoutcast-network-writer",
            daemon=True,
        )
        self._writer_thread.start()
        self._network_thread.start()

    def ensure_started(self, cfg: StationPipelineConfig):
        signature = self._cfg_signature(cfg)
        if self.is_running() and self._signature == signature:
            return self._process
        self.stop()
        host = str(cfg.icecast_host or "").strip()
        port = int(cfg.icecast_port or 0)
        if not host or not 1 <= port <= 65535:
            raise ShoutcastProtocolError("SHOUTcast source destination is invalid")
        source_socket = None
        try:
            source_socket = self._socket_factory(
                (host, port),
                timeout=self._connect_timeout_sec,
            )
            perform_shoutcast_v1_handshake(
                source_socket,
                cfg,
                timeout_seconds=self._handshake_timeout_sec,
            )
            source_socket.settimeout(self._write_timeout_sec)
            command = build_ffmpeg_encoded_sink_cmd(cfg, self.ffmpeg_bin)
            process = self._spawn_process(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.1)
            if process.poll() is not None:
                raise ShoutcastProtocolError(
                    "SHOUTcast encoder exited during startup"
                )
            self._socket = source_socket
            self._process = process
            self._signature = signature
            with self._lock:
                self._handshake_accepted = True
                self._network_failed = False
                self._writer_failed = False
                self._writer_backpressured = False
                self._dropped_pcm_chunks = 0
                self._encoded_bytes_sent = 0
                self._last_pcm_write_monotonic = None
                self._last_network_write_monotonic = None
            self._start_threads()
            _log.info(
                "Started SHOUTcast legacy source host=%s port=%s profile=%s",
                host,
                port,
                cfg.stream_codec_profile,
            )
            return process
        except Exception:
            if source_socket is not None and source_socket is not self._socket:
                try:
                    source_socket.close()
                except Exception:
                    pass
            self.stop()
            raise

    def stop(self, *, preserve_probe_state: bool = False) -> None:
        del preserve_probe_state
        self._stop_event.set()
        source_socket = self._socket
        self._socket = None
        if source_socket is not None:
            try:
                source_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                source_socket.close()
            except Exception:
                pass
        process = self._process
        self._process = None
        self._signature = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass
        for stream_name in ("stdin", "stdout"):
            stream = getattr(process, stream_name, None) if process else None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        for thread in (self._writer_thread, self._network_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._writer_thread = None
        self._network_thread = None
        self._clear_queue()
        with self._lock:
            self._handshake_accepted = False
            self._network_failed = False
            self._writer_failed = False
