import http.client
import logging
import queue
import re
import ssl
import subprocess
import threading
import time
import zlib
from dataclasses import replace
from typing import Callable

from app.audio.ffmpeg_pipeline import build_ffmpeg_encoded_sink_cmd
from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.icecast_source_transport import IcecastSourceTransport

_log = logging.getLogger(__name__)

# 4096 bytes is about 21.3 ms of 48 kHz stereo s16 PCM.  Keep enough queued
# audio to ride through a short upstream/TCP pause without deleting already
# scheduled programme audio.  The previous 64-chunk queue was only ~1.36 s;
# on saturation it discarded the whole queue, producing audible multi-second
# jumps.  1024 chunks is ~21.8 s and about 4 MiB per active sink. The cache
# prefetch path prevents cold-volume stalls from filling this reserve.
# Keep roughly 44 seconds of per-mount PCM reserve. This absorbs Windows
# encoder/TCP startup stalls without deleting the already scheduled song.
_PCM_QUEUE_MAX_CHUNKS = 1024
# FLAC is lossless and its Ogg pages can briefly need more write-side reserve
# on a busy origin. Keep the larger reserve only on the two FLAC branches so
# AAC and local programme timing remain unchanged.
_PCM_FLAC_QUEUE_MAX_CHUNKS = 2048
_PCM_INITIAL_PROGRAMME_GRACE_SECONDS = 0.25
_PCM_PROGRAMME_START_RESERVE_BYTES = 48 * 1024
_PCM_PROGRAMME_START_MAX_WAIT_SECONDS = 0.25
# When an origin is down, keep the newest two seconds before a reconnect.  A
# failed branch must never block the shared station fan-out or force healthy
# sibling mounts to underrun.
_PCM_LIVE_RESYNC_CHUNKS = 96
_PCM_BYTES_PER_SECOND = 48_000 * 2 * 2
_PCM_LIVE_RESYNC_BYTES = 3 * _PCM_BYTES_PER_SECOND
_PCM_CONTINUITY_CHUNK_BYTES = 4 * 1024
_PCM_CONTINUITY_INTERVAL_SECONDS = (
    _PCM_CONTINUITY_CHUNK_BYTES / _PCM_BYTES_PER_SECOND
)
_ENCODED_CHUNK_BYTES = 4 * 1024
_ENCODER_ERROR_TOKENS = (
    "error",
    "failed",
    "broken",
    "refused",
    "reset",
    "unauthorized",
    "forbidden",
    "timed out",
    "closed",
    "invalid",
    "server returned",
    "end of file",
)


def current_codec_fallback(
    cfg: StationPipelineConfig,
) -> StationPipelineConfig | None:
    """Return the already-proven legacy profile for a new AAC policy profile."""

    token = str(cfg.stream_codec_profile or "").strip().lower().replace("-", "_")
    if token.startswith("aac_low"):
        return replace(
            cfg,
            stream_codec_profile="he_aac_192",
            stream_bitrate_kbps=192,
        )
    if token.startswith(("aac_he_v2", "he_aac_v2")):
        return replace(
            cfg,
            stream_codec_profile="he_aac_96",
            stream_bitrate_kbps=96,
        )
    return None


def _mount_spread_seconds(
    cfg: StationPipelineConfig,
    maximum_seconds: float,
) -> float:
    """Return a stable per-mount delay that prevents synchronized reconnects."""

    maximum = max(0.0, float(maximum_seconds))
    if maximum <= 0:
        return 0.0
    identity = (
        f"{str(cfg.icecast_host or '').casefold()}:"
        f"{int(cfg.icecast_port or 0)}:"
        f"{str(cfg.icecast_mount or '').casefold()}"
    ).encode("utf-8", "replace")
    fraction = (zlib.crc32(identity) & 0xFFFFFFFF) / 0xFFFFFFFF
    return maximum * fraction


def _retry_spread_window_seconds(base_delay: float) -> float:
    """Spread a reconnect wave widely enough for a small Windows origin."""

    return min(30.0, max(8.0, float(base_delay)))


_CREDENTIAL_URL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:\s]+:)[^@\s]+@")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*(?:basic|bearer)\s+)\S+")


def probe_icecast_mount(cfg: StationPipelineConfig, timeout: float = 2.0) -> bool:
    """Confirm that the configured source has created a readable mount."""

    host = str(cfg.icecast_host or "").strip()
    port = int(cfg.icecast_port or 0)
    mount = str(cfg.icecast_mount or "").strip()
    if not host or port <= 0 or not mount:
        return False
    if not mount.startswith("/"):
        mount = f"/{mount}"

    connection = None
    try:
        if bool(getattr(cfg, "icecast_tls_enabled", False)):
            connection = http.client.HTTPSConnection(
                host,
                port,
                timeout=max(0.1, float(timeout)),
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                host,
                port,
                timeout=max(0.1, float(timeout)),
            )
        connection.request(
            "GET",
            mount,
            headers={
                "User-Agent": "RadioTEDU-OnAir-Mount-Probe/1.0",
                "Icy-MetaData": "0",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        content_type = str(response.getheader("Content-Type") or "").lower()
        if int(response.status) not in {200, 206}:
            return False
        if not (
            content_type.startswith("audio/")
            or content_type in {"application/ogg", "video/ogg"}
        ):
            return False
        # Header completion is the authoritative mount-presence signal. AAC
        # streaming responses are intentionally endless and small encoders may
        # not yield a body byte inside this short control-plane timeout.
        return True
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class IcecastAudioSink:
    protocol = "icecast"
    # This sink owns the final PCM clock and writes just-in-time continuity
    # frames when its programme queue is genuinely empty.  Upstream runtime
    # filler must not be queued behind real audio: doing so turns a harmless
    # decoder handoff into a delayed audible silence notch.
    manages_pcm_continuity = True

    def __init__(
        self,
        ffmpeg_bin: str,
        spawn_process: Callable[..., subprocess.Popen],
        *,
        mount_probe: Callable[[StationPipelineConfig], bool] | None = None,
        probe_interval_sec: float = 15.0,
        probe_warmup_sec: float = 3.0,
        probe_failure_threshold: int = 2,
        reconnect_failure_threshold: int = 12,
        source_factory: Callable[[StationPipelineConfig], object] = IcecastSourceTransport,
        initial_connect_spread_sec: float = 0.0,
        drop_on_backpressure: bool = True,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self._spawn_process = spawn_process
        self._process = None
        self._signature = None
        self._cfg = None
        self._mount_probe = mount_probe
        self._probe_interval_sec = max(0.05, float(probe_interval_sec))
        self._probe_warmup_sec = max(0.0, float(probe_warmup_sec))
        self._probe_failure_threshold = max(1, int(probe_failure_threshold))
        self._reconnect_failure_threshold = max(
            self._probe_failure_threshold,
            int(reconnect_failure_threshold),
        )
        self._source_factory = source_factory
        self._initial_connect_spread_sec = max(
            0.0, float(initial_connect_spread_sec)
        )
        # Legacy/unit callers can retain the bounded live-resync policy. The
        # real station runtime disables destructive drops so an encoder stall
        # pauses the producer instead of jumping to a later PCM timestamp.
        self._drop_on_backpressure = bool(drop_on_backpressure)
        self._source = None
        self._connector_thread = None
        self._network_failed = False
        self._encoded_bytes_sent = 0
        self._last_network_write_monotonic = None
        self._last_network_error = ""
        self._network_error_count = 0
        self._probe_stop = threading.Event()
        self._probe_thread = None
        self._probe_lock = threading.Lock()
        self._mount_healthy = None
        self._probe_failures = 0
        self._pcm_queue_capacity_chunks = _PCM_QUEUE_MAX_CHUNKS
        self._pcm_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=self._pcm_queue_capacity_chunks
        )
        self._writer_stop = threading.Event()
        self._writer_thread = None
        self._writer_lock = threading.Lock()
        self._writer_failed = False
        self._writer_backpressured = False
        self._writer_backpressure_started_monotonic = None
        self._writer_dropped_chunks = 0
        self._writer_silence_chunks = 0
        self._last_write_monotonic = None
        self._stderr_stop = threading.Event()
        self._stderr_thread = None
        self._stderr_lock = threading.Lock()
        self._last_encoder_error = ""
        self._encoder_error_count = 0
        self._effective_stream_codec_profile = ""
        self._requested_stream_codec_profile = ""
        self._profile_fallback_active = False

    @property
    def process(self):
        return self._process

    @property
    def stdin(self):
        if not self._process:
            return None
        return getattr(self._process, "stdin", None)

    def _cfg_signature(self, cfg: StationPipelineConfig) -> tuple[str, int, str, str, str]:
        mount = str(cfg.icecast_mount or "").strip()
        if mount and not mount.startswith("/"):
            mount = f"/{mount}"
        return (
            str(cfg.icecast_host or "").strip(),
            int(cfg.icecast_port),
            mount or "/stream",
            str(cfg.icecast_user or "").strip(),
            str(cfg.icecast_password or ""),
        )

    def is_running(self) -> bool:
        """Return process liveness without turning probe noise into teardown.

        Listener probes are external evidence and may fail transiently when a
        proxy resets a short GET after returning audio.  Treating that signal
        as process death caused a healthy source to be destroyed and recreated
        every few seconds.  The probe remains visible in ``health_snapshot``;
        encoder exit and PCM-writer failure remain the restart authorities.
        """

        return bool(
            (self._process and self._process.poll() is None)
            or (
                self._connector_thread
                and self._connector_thread.is_alive()
                and not self._writer_stop.is_set()
            )
        )

    def accepts_input(self) -> bool:
        """Return whether the encoder process can accept queued PCM.

        Mount health is deliberately excluded.  A restarted FFmpeg encoder
        needs PCM before it can create the remote mount, so gating input on a
        successful mount probe creates an unrecoverable deadlock.
        """

        # Local programme generation is authoritative. During an origin
        # outage there may be no encoder stdin yet; accepting the chunk here
        # keeps sibling/local timelines healthy while the connector retries.
        return bool(
            self.is_running()
            and (
                self.stdin is None
                or (self._process and self._process.poll() is None)
            )
        )

    def write_pcm(self, chunk: bytes) -> bool:
        """Queue PCM without allowing a blocked network encoder to stall playout."""

        if not chunk or not self.accepts_input():
            return False
        payload = bytes(chunk)
        try:
            self._pcm_queue.put_nowait(payload)
        except queue.Full:
            if not self._drop_on_backpressure:
                try:
                    self._pcm_queue.put(payload, timeout=0.05)
                    return True
                except queue.Full:
                    with self._writer_lock:
                        self._writer_backpressured = True
                        if self._writer_backpressure_started_monotonic is None:
                            self._writer_backpressure_started_monotonic = time.monotonic()
                        self._writer_dropped_chunks += 1
                    # Keep the already scheduled programme reserve intact.
                    # The caller will retry on the next PCM frame after the
                    # encoder writer makes room.
                    return False
            # This mount is already far behind. Resync only this failed branch
            # to a byte-measured three-second live window; chunk counts vary by
            # 4x on Windows and previously caused unpredictable song cuts.
            dropped = 0
            queued_bytes = self._queued_pcm_bytes()
            while (
                queued_bytes > _PCM_LIVE_RESYNC_BYTES
                or self._pcm_queue.full()
            ):
                try:
                    stale = self._pcm_queue.get_nowait()
                    queued_bytes -= len(stale)
                    dropped += 1
                except queue.Empty:
                    break
            try:
                self._pcm_queue.put_nowait(payload)
            except queue.Full:
                dropped += 1
            with self._writer_lock:
                self._writer_backpressured = True
                if self._writer_backpressure_started_monotonic is None:
                    self._writer_backpressure_started_monotonic = time.monotonic()
                self._writer_dropped_chunks += max(1, dropped)
            return False
        return True

    def health_snapshot(self) -> dict:
        queued_pcm_bytes = self._queued_pcm_bytes()
        with self._probe_lock, self._writer_lock, self._stderr_lock:
            process_running = bool(
                self._process and self._process.poll() is None
            )
            last_write_age = (
                None
                if self._last_write_monotonic is None
                else max(0.0, time.monotonic() - self._last_write_monotonic)
            )
            backpressure_age = (
                None
                if self._writer_backpressure_started_monotonic is None
                else max(
                    0.0,
                    time.monotonic()
                    - self._writer_backpressure_started_monotonic,
                )
            )
            last_network_write_age = (
                None
                if self._last_network_write_monotonic is None
                else max(
                    0.0,
                    time.monotonic() - self._last_network_write_monotonic,
                )
            )
            mount_healthy = self._mount_healthy
            if self._mount_probe is None:
                if self._network_failed:
                    mount_healthy = False
                elif process_running and last_network_write_age is not None:
                    mount_healthy = last_network_write_age <= 10.0
                else:
                    mount_healthy = None
            return {
                "process_running": process_running,
                "mount_healthy": mount_healthy,
                "consecutive_probe_failures": int(self._probe_failures),
                "writer_running": bool(
                    self._writer_thread and self._writer_thread.is_alive()
                ),
                "writer_failed": bool(self._writer_failed),
                "writer_backpressured": bool(self._writer_backpressured),
                "writer_backpressure_age_seconds": (
                    None
                    if backpressure_age is None
                    else round(backpressure_age, 3)
                ),
                "queued_pcm_chunks": int(self._pcm_queue.qsize()),
                "queued_pcm_bytes": int(queued_pcm_bytes),
                "queued_pcm_seconds": round(
                    queued_pcm_bytes / _PCM_BYTES_PER_SECOND,
                    3,
                ),
                "pcm_queue_capacity_chunks": int(self._pcm_queue_capacity_chunks),
                "dropped_pcm_chunks": int(self._writer_dropped_chunks),
                "continuity_silence_chunks": int(self._writer_silence_chunks),
                "last_write_age_seconds": (
                    None if last_write_age is None else round(last_write_age, 3)
                ),
                "last_encoder_error": self._last_encoder_error,
                "encoder_error_count": int(self._encoder_error_count),
                "requested_stream_codec_profile": self._requested_stream_codec_profile,
                "effective_stream_codec_profile": self._effective_stream_codec_profile,
                "profile_fallback_active": bool(self._profile_fallback_active),
                "network_writer_running": bool(
                    self._connector_thread and self._connector_thread.is_alive()
                ),
                "network_failed": bool(self._network_failed),
                "encoded_bytes_sent": int(self._encoded_bytes_sent),
                "last_network_write_age_seconds": (
                    None
                    if last_network_write_age is None
                    else round(last_network_write_age, 3)
                ),
                "last_network_error": self._last_network_error,
                "network_error_count": int(self._network_error_count),
            }

    @staticmethod
    def _sanitize_encoder_line(line: object, cfg: StationPipelineConfig) -> str:
        if isinstance(line, bytes):
            text = line.decode("utf-8", errors="replace")
        else:
            text = str(line or "")
        password = str(getattr(cfg, "icecast_password", "") or "")
        if password:
            text = text.replace(password, "<redacted>")
        text = _CREDENTIAL_URL_RE.sub(r"\1<redacted>@", text)
        text = _AUTH_HEADER_RE.sub(r"\1<redacted>", text)
        return " ".join(text.split())[:500]

    def _start_stderr_worker(self, cfg: StationPipelineConfig) -> None:
        proc = self._process
        stderr = getattr(proc, "stderr", None) if proc is not None else None
        if stderr is None:
            return
        self._stderr_stop.clear()

        def run() -> None:
            while not self._stderr_stop.is_set():
                try:
                    line = stderr.readline()
                except Exception:
                    return
                if not line:
                    return
                safe = self._sanitize_encoder_line(line, cfg)
                if not safe or not any(token in safe.casefold() for token in _ENCODER_ERROR_TOKENS):
                    continue
                with self._stderr_lock:
                    self._last_encoder_error = safe
                    self._encoder_error_count += 1

        self._stderr_thread = threading.Thread(
            target=run,
            name="icecast-encoder-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _clear_pcm_queue(self) -> None:
        while True:
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                return

    def _queued_pcm_bytes(self) -> int:
        # Queue.qsize() counts pipe reads, whose byte lengths vary on Windows.
        # Inspect under the queue mutex so reserve is measured in audio time.
        with self._pcm_queue.mutex:
            return sum(len(chunk) for chunk in self._pcm_queue.queue)

    def _trim_pcm_queue_to_latest(self, maximum_chunks: int) -> int:
        keep = max(1, int(maximum_chunks))
        dropped = 0
        while self._pcm_queue.qsize() > keep:
            try:
                self._pcm_queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            with self._writer_lock:
                self._writer_dropped_chunks += dropped
                self._writer_backpressured = True
                if self._writer_backpressure_started_monotonic is None:
                    self._writer_backpressure_started_monotonic = time.monotonic()
        return dropped

    def _trim_pcm_queue_to_latest_bytes(self, maximum_bytes: int) -> int:
        maximum = max(_PCM_CONTINUITY_CHUNK_BYTES, int(maximum_bytes))
        dropped = 0
        queued_bytes = self._queued_pcm_bytes()
        while queued_bytes > maximum:
            try:
                stale = self._pcm_queue.get_nowait()
                queued_bytes -= len(stale)
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            with self._writer_lock:
                self._writer_dropped_chunks += dropped
                self._writer_backpressured = True
                if self._writer_backpressure_started_monotonic is None:
                    self._writer_backpressure_started_monotonic = time.monotonic()
        return dropped

    def _start_writer_worker(self) -> None:
        self._writer_stop.clear()
        self._clear_pcm_queue()
        with self._writer_lock:
            self._writer_failed = False
            self._writer_backpressured = False
            self._writer_backpressure_started_monotonic = None
            self._writer_dropped_chunks = 0
            self._writer_silence_chunks = 0
            self._last_write_monotonic = None

        def run() -> None:
            silence_chunk = b"\x00" * _PCM_CONTINUITY_CHUNK_BYTES
            continuity_seconds = _PCM_CONTINUITY_CHUNK_BYTES / _PCM_BYTES_PER_SECOND
            next_write = time.monotonic()
            output_clock_started = False
            programme_started = False
            initial_grace_deadline = None
            first_programme_queued_at = None
            while not self._writer_stop.is_set():
                # Never remove programme audio while the encoder/source is
                # reconnecting.  The bounded queue is the continuity reserve
                # that lets a short origin failure recover without skipping
                # forward in the station timeline.
                stdin = self.stdin
                if stdin is None or not self.accepts_input():
                    # Do not carry a synthetic-silence deadline across an
                    # encoder reconnect. The first frame starts a fresh clock.
                    next_write = time.monotonic()
                    output_clock_started = False
                    programme_started = False
                    initial_grace_deadline = None
                    first_programme_queued_at = None
                    self._writer_stop.wait(0.01)
                    continue

                starting = not programme_started
                if starting:
                    now = time.monotonic()
                    if initial_grace_deadline is None:
                        initial_grace_deadline = (
                            now + _PCM_INITIAL_PROGRAMME_GRACE_SECONDS
                        )
                    queued_bytes = self._queued_pcm_bytes()
                    if queued_bytes > 0 and first_programme_queued_at is None:
                        first_programme_queued_at = now
                    if queued_bytes <= 0:
                        first_programme_queued_at = None
                    reserve_ready = bool(
                        queued_bytes >= _PCM_PROGRAMME_START_RESERVE_BYTES
                        or (
                            queued_bytes > 0
                            and first_programme_queued_at is not None
                            and now - first_programme_queued_at
                            >= _PCM_PROGRAMME_START_MAX_WAIT_SECONDS
                        )
                    )
                    reserve_wait_remaining = (
                        _PCM_PROGRAMME_START_MAX_WAIT_SECONDS
                        - (now - first_programme_queued_at)
                        if first_programme_queued_at is not None
                        else 0.0
                    )
                    if not reserve_ready and (
                        now < initial_grace_deadline
                        or (
                            not output_clock_started
                            and queued_bytes > 0
                            and reserve_wait_remaining > 0
                        )
                    ):
                        self._writer_stop.wait(
                            min(
                                0.005,
                                max(
                                    0.0,
                                    initial_grace_deadline - now,
                                    reserve_wait_remaining,
                                ),
                            )
                        )
                        continue

                # The PCM producer can deliver short bursts. TCP/pipe
                # backpressure is not a media clock, so take exactly one frame
                # per deadline and prefer programme audio to filler.
                now = time.monotonic()
                if (
                    output_clock_started
                    and now < next_write
                    and self._writer_stop.wait(next_write - now)
                ):
                    return
                if starting:
                    queued_bytes = self._queued_pcm_bytes()
                    reserve_ready = bool(
                        queued_bytes >= _PCM_PROGRAMME_START_RESERVE_BYTES
                        or (
                            queued_bytes > 0
                            and first_programme_queued_at is not None
                            and time.monotonic() - first_programme_queued_at
                            >= _PCM_PROGRAMME_START_MAX_WAIT_SECONDS
                        )
                    )
                else:
                    reserve_ready = True
                if reserve_ready:
                    try:
                        chunk = self._pcm_queue.get_nowait()
                        silence = False
                    except queue.Empty:
                        chunk = silence_chunk
                        silence = True
                    if not silence:
                        programme_started = True
                else:
                    chunk = silence_chunk
                    silence = True
                try:
                    stdin.write(chunk)
                    flush = getattr(stdin, "flush", None)
                    if callable(flush):
                        flush()
                    with self._writer_lock:
                        self._writer_failed = False
                        self._last_write_monotonic = time.monotonic()
                        clear_threshold = (
                            _PCM_LIVE_RESYNC_CHUNKS
                            if self._drop_on_backpressure
                            else max(
                                _PCM_LIVE_RESYNC_CHUNKS,
                                self._pcm_queue_capacity_chunks // 2,
                            )
                        )
                        if not silence and self._pcm_queue.qsize() < clear_threshold:
                            self._writer_backpressured = False
                            self._writer_backpressure_started_monotonic = None
                        if silence:
                            self._writer_silence_chunks += 1
                except Exception:
                    with self._writer_lock:
                        self._writer_failed = True
                    return
                wrote_at = time.monotonic()
                frame_seconds = len(chunk) / _PCM_BYTES_PER_SECOND
                # Sustained output cannot outrun its PCM clock. Permit at most
                # one frame of catch-up after a late Windows scheduling quantum
                # instead of draining the entire queue into TinyIce as a burst.
                next_write = (
                    wrote_at + frame_seconds
                    if not output_clock_started
                    else max(next_write + frame_seconds, wrote_at)
                )
                output_clock_started = True

        self._writer_thread = threading.Thread(
            target=run,
            name="icecast-pcm-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _close_encoder_connection(self) -> None:
        source = self._source
        self._source = None
        if source is not None:
            try:
                source.close()
            except Exception:
                pass
        proc = self._process
        self._process = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(proc, stream_name, None) if proc else None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        # A network reconnect must not erase programme audio which the runtime
        # has already handed to us.  The bounded queue preserves a short bridge
        # across transient source-server failures; stop() performs the final
        # queue cleanup after both workers have exited.

    def _start_connector_worker(self, cfg: StationPipelineConfig) -> None:
        def run() -> None:
            effective_cfg = cfg
            fallback_cfg = current_codec_fallback(cfg)
            delay_index = 0
            delays = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
            initial_delay = _mount_spread_seconds(
                cfg, self._initial_connect_spread_sec
            )
            if initial_delay > 0.0 and self._writer_stop.wait(initial_delay):
                return
            while not self._writer_stop.is_set():
                connected_at = None
                delivered_this_connection = False
                try:
                    source = self._source_factory(effective_cfg)
                    if self._writer_stop.is_set():
                        source.close()
                        return
                    # A reconnect creates a fresh public stream clock. Keep
                    # only the newest live reserve so the mount returns near
                    # the current programme instead of staying far behind.
                    self._trim_pcm_queue_to_latest_bytes(_PCM_LIVE_RESYNC_BYTES)
                    command = build_ffmpeg_encoded_sink_cmd(
                        effective_cfg, self.ffmpeg_bin
                    )
                    proc = self._spawn_process(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self._source = source
                    self._process = proc
                    self._effective_stream_codec_profile = str(
                        effective_cfg.stream_codec_profile or ""
                    )
                    self._start_stderr_worker(effective_cfg)
                    time.sleep(0.1)
                    if proc.poll() is not None:
                        raise RuntimeError("Icecast encoder exited during startup")
                    connected_at = time.monotonic()
                    encoded = getattr(proc, "stdout", None)
                    if encoded is None:
                        raise RuntimeError("Icecast encoder output is unavailable")
                    while not self._writer_stop.is_set():
                        chunk = encoded.read(_ENCODED_CHUNK_BYTES)
                        if not chunk:
                            if proc.poll() is None:
                                time.sleep(0.01)
                                continue
                            raise RuntimeError("Icecast encoder stopped")
                        source.send(chunk)
                        delivered_this_connection = True
                        with self._writer_lock:
                            self._network_failed = False
                            self._encoded_bytes_sent += len(chunk)
                            self._last_network_write_monotonic = time.monotonic()
                            self._last_network_error = ""
                except Exception as exc:
                    safe = self._sanitize_encoder_line(exc, effective_cfg)
                    with self._writer_lock:
                        self._network_failed = True
                        self._last_network_error = safe
                        self._network_error_count += 1
                    if (
                        not delivered_this_connection
                        and fallback_cfg is not None
                        and effective_cfg is cfg
                    ):
                        effective_cfg = fallback_cfg
                        delay_index = 0
                        self._profile_fallback_active = True
                        self._effective_stream_codec_profile = str(
                            effective_cfg.stream_codec_profile or ""
                        )
                finally:
                    if connected_at is not None and time.monotonic() - connected_at >= 30.0:
                        delay_index = 0
                    self._close_encoder_connection()
                base_delay = delays[min(delay_index, len(delays) - 1)]
                if delivered_this_connection:
                    # A previously healthy single mount needs prompt recovery,
                    # not the broad cold-start spread used for an origin outage.
                    retry_delay = 0.5 + _mount_spread_seconds(cfg, 1.0)
                else:
                    retry_delay = base_delay + _mount_spread_seconds(
                        cfg,
                        _retry_spread_window_seconds(base_delay),
                    )
                if self._writer_stop.wait(retry_delay):
                    return
                delay_index += 1

        self._connector_thread = threading.Thread(
            target=run,
            name="icecast-source-connector",
            daemon=True,
        )
        self._connector_thread.start()

    def _start_probe_worker(
        self,
        cfg: StationPipelineConfig,
        *,
        preserve_failure_state: bool = False,
    ) -> None:
        if self._mount_probe is None:
            return
        self._probe_stop.clear()
        if not preserve_failure_state:
            with self._probe_lock:
                self._mount_healthy = None
                self._probe_failures = 0

        def run() -> None:
            initial_delay = self._probe_warmup_sec + _mount_spread_seconds(
                cfg,
                min(5.0, self._probe_interval_sec * 0.5),
            )
            if self._probe_stop.wait(initial_delay):
                return
            while not self._probe_stop.is_set():
                try:
                    healthy = bool(self._mount_probe(cfg))
                except Exception:
                    healthy = False
                request_reconnect = False
                with self._probe_lock:
                    if healthy:
                        self._probe_failures = 0
                        self._mount_healthy = True
                    else:
                        self._probe_failures += 1
                        if self._probe_failures >= self._probe_failure_threshold:
                            self._mount_healthy = False
                        request_reconnect = (
                            self._probe_failures
                            >= self._reconnect_failure_threshold
                            and self._probe_failures
                            % self._reconnect_failure_threshold
                            == 0
                        )
                if request_reconnect:
                    # A listener mount can disappear while the source socket
                    # still accepts buffered writes. Terminating only the local
                    # encoder unblocks the connector and re-registers the mount;
                    # transient probe noise below this threshold remains passive.
                    proc = self._process
                    if proc is not None and proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                if self._probe_stop.wait(self._probe_interval_sec):
                    return

        self._probe_thread = threading.Thread(
            target=run,
            name="icecast-mount-probe",
            daemon=True,
        )
        self._probe_thread.start()

    def ensure_started(self, cfg: StationPipelineConfig):
        signature = self._cfg_signature(cfg)
        if self.is_running() and self._signature == signature:
            return self._process
        self.stop(preserve_probe_state=False)
        profile = str(cfg.stream_codec_profile or "").strip().lower()
        queue_capacity = (
            _PCM_FLAC_QUEUE_MAX_CHUNKS
            if profile.startswith(("ogg_flac", "flac_ogg"))
            else _PCM_QUEUE_MAX_CHUNKS
        )
        if queue_capacity != self._pcm_queue_capacity_chunks:
            self._pcm_queue_capacity_chunks = queue_capacity
            self._pcm_queue = queue.Queue(maxsize=queue_capacity)
        _log.info(
            "Starting Icecast sink host=%s port=%s mount=%s user=%s",
            cfg.icecast_host,
            cfg.icecast_port,
            cfg.icecast_mount,
            cfg.icecast_user,
        )
        self._signature = signature
        self._cfg = cfg
        self._requested_stream_codec_profile = str(cfg.stream_codec_profile or "")
        self._effective_stream_codec_profile = str(cfg.stream_codec_profile or "")
        self._profile_fallback_active = False
        self._start_writer_worker()
        self._start_connector_worker(cfg)
        self._start_probe_worker(
            cfg,
            preserve_failure_state=False,
        )
        # Keep connector readiness bounded independently from the playout
        # clock. Crossfade tests and deterministic schedulers can replace
        # time.monotonic(), but that must never turn sink startup into an
        # unbounded wait.
        for _ in range(35):
            if self._process is not None:
                break
            time.sleep(0.01)
        return self._process

    def stop(self, *, preserve_probe_state: bool = False) -> None:
        self._probe_stop.set()
        self._writer_stop.set()
        self._stderr_stop.set()
        self._signature = None
        self._cfg = None
        self._close_encoder_connection()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=3.0)
        self._probe_thread = None
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=3.0)
        self._writer_thread = None
        if self._connector_thread is not None:
            self._connector_thread.join(timeout=4.0)
        self._connector_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=3.0)
        self._stderr_thread = None
        self._clear_pcm_queue()
        if not preserve_probe_state:
            with self._probe_lock:
                self._mount_healthy = None
                self._probe_failures = 0
