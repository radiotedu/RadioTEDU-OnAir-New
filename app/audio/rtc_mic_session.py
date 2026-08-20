import logging
import math
import threading
import time
from collections import deque

logger = logging.getLogger("cleanroom.webrtc")

try:
    from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    _AIORTC_AVAILABLE = True
except ImportError:
    _AIORTC_AVAILABLE = False

PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_CHUNK_BYTES = 4096


def aiortc_available() -> bool:
    return _AIORTC_AVAILABLE


class RtcMicSession:
    """WebRTC-based mic session with the same read_pcm() interface as MicSession."""

    def __init__(self, station_id: int, max_buffer_bytes: int = 96000, return_track=None) -> None:
        self.station_id = int(station_id)
        self.max_buffer_bytes = max(1, int(max_buffer_bytes))
        self._pc = None
        self._buffer = deque()
        self._buffer_bytes = 0
        self._lock = threading.Lock()
        self._running = False
        self._receiving = False
        self._last_chunk_at = 0.0
        self._level_db = -60.0
        self._peak_db = -60.0
        self._last_error = ""
        self._on_connection_failed = None
        self._return_track = return_track

    @property
    def running(self) -> bool:
        return bool(self._running)

    def _append_pcm(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._buffer.append(bytes(chunk))
            self._buffer_bytes += len(chunk)
            while self._buffer_bytes > self.max_buffer_bytes and self._buffer:
                dropped = self._buffer.popleft()
                self._buffer_bytes -= len(dropped)
        self._receiving = True
        self._last_chunk_at = time.monotonic()
        self._update_levels(chunk)

    def _update_levels(self, chunk: bytes) -> None:
        if len(chunk) < 2:
            return
        sample_count = len(chunk) // 2
        if sample_count <= 0:
            return
        values = memoryview(chunk).cast("h")
        peak = max(abs(int(sample)) for sample in values) if values else 0
        if peak <= 0:
            self._peak_db = -60.0
            self._level_db = -60.0
            return
        rms = math.sqrt(sum(int(sample) * int(sample) for sample in values) / sample_count)
        self._peak_db = max(-60.0, 20.0 * math.log10(peak / 32767.0))
        self._level_db = max(-60.0, 20.0 * math.log10(max(rms, 1.0) / 32767.0))

    async def set_offer(self, sdp: str, ice_servers: list[dict] | None = None) -> str:
        if not _AIORTC_AVAILABLE:
            raise RuntimeError("aiortc is not installed")

        config = None
        if ice_servers:
            config = RTCConfiguration(iceServers=[
                RTCIceServer(
                    urls=s.get("urls", ""),
                    username=s.get("username", ""),
                    credential=s.get("credential", ""),
                )
                for s in ice_servers
            ])
        self._pc = RTCPeerConnection(configuration=config) if config else RTCPeerConnection()
        logger.info("WebRTC offer received for station %d", self.station_id)

        @self._pc.on("track")
        async def on_track(track):
            logger.info("WebRTC track received: %s for station %d", track.kind, self.station_id)
            if track.kind != "audio":
                return
            import asyncio
            asyncio.create_task(self._consume_track(track))

        @self._pc.on("connectionstatechange")
        async def on_state_change():
            state = self._pc.connectionState
            logger.info("WebRTC connection state: %s for station %d", state, self.station_id)
            if state in ("failed", "closed"):
                self._running = False
                self._receiving = False
                if self._on_connection_failed:
                    self._on_connection_failed(self.station_id)

        offer = RTCSessionDescription(sdp=sdp, type="offer")
        await self._pc.setRemoteDescription(offer)
        if self._return_track is not None:
            self._pc.addTrack(self._return_track)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        self._running = True
        logger.info("WebRTC answer generated for station %d", self.station_id)
        return self._pc.localDescription.sdp

    async def _consume_track(self, track) -> None:
        import numpy as np

        while self._running:
            try:
                frame = await track.recv()
            except Exception:
                break
            # aiortc delivers av.AudioFrame in s16 format (int16 samples).
            # frame.to_ndarray() returns shape (channels, samples) with dtype int16.
            raw = frame.to_ndarray()
            if raw.ndim > 1:
                raw = raw.mean(axis=0).astype(np.int16)
            if raw.dtype != np.int16:
                raw = (np.clip(raw, -1.0, 1.0) * 32767).astype(np.int16)
            if frame.sample_rate != PCM_SAMPLE_RATE:
                logger.warning(
                    "Unexpected sample rate %d (expected %d) for station %d",
                    frame.sample_rate, PCM_SAMPLE_RATE, self.station_id,
                )
            self._append_pcm(raw.tobytes())

    async def add_ice_candidate(self, candidate: dict) -> None:
        if self._pc is None:
            return
        from aiortc.sdp import candidate_from_sdp

        sdp_line = str(candidate.get("candidate", "")).strip()
        if not sdp_line:
            return
        parsed = candidate_from_sdp(sdp_line)
        parsed.sdpMid = candidate.get("sdpMid")
        parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
        await self._pc.addIceCandidate(parsed)

    def read_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested == 0:
            return b""
        output = bytearray()
        with self._lock:
            while self._buffer and len(output) < requested:
                chunk = self._buffer[0]
                need = requested - len(output)
                if len(chunk) <= need:
                    output.extend(self._buffer.popleft())
                    self._buffer_bytes -= len(chunk)
                    continue
                output.extend(chunk[:need])
                self._buffer[0] = chunk[need:]
                self._buffer_bytes -= need
        if len(output) < requested:
            output.extend(b"\x00" * (requested - len(output)))
        return bytes(output)

    def snapshot(self) -> dict:
        return {
            "station_id": self.station_id,
            "running": bool(self._running),
            "receiving": bool(self._receiving),
            "buffer_bytes": int(self._buffer_bytes),
            "level_db": float(self._level_db),
            "peak_db": float(self._peak_db),
            "last_chunk_at": float(self._last_chunk_at or 0.0),
            "last_error": str(self._last_error or ""),
        }

    async def stop(self) -> None:
        self._running = False
        self._receiving = False
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
        logger.info("WebRTC session stopped for station %d", self.station_id)
