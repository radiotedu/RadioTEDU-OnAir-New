from __future__ import annotations

import math
import threading
import time
from collections import deque

from app.db import get_connection, init_db


def _sample(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        return 0
    return int.from_bytes(data[offset : offset + 2], "little", signed=True)


def _clamp(value: float) -> int:
    return max(-32768, min(32767, int(round(value))))


class GuestReturnBuffer:
    def __init__(self, max_bytes: int = 384000):
        self._max_bytes = max_bytes
        self._chunks = deque()
        self._size = 0
        self._lock = threading.Lock()

    def push(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._chunks.append(bytes(chunk))
            self._size += len(chunk)
            while self._size > self._max_bytes and self._chunks:
                self._size -= len(self._chunks.popleft())

    def read(self, requested: int) -> bytes:
        output = bytearray()
        with self._lock:
            while self._chunks and len(output) < requested:
                chunk = self._chunks[0]
                need = requested - len(output)
                if len(chunk) <= need:
                    output.extend(self._chunks.popleft())
                    self._size -= len(chunk)
                else:
                    output.extend(chunk[:need])
                    self._chunks[0] = chunk[need:]
                    self._size -= need
        if len(output) < requested:
            output.extend(b"\x00" * (requested - len(output)))
        return bytes(output)


class GuestAudioRegistry:
    """Mixes admitted guest sources and maintains per-guest program-minus return buffers."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[int, object] = {}
        self._returns: dict[int, GuestReturnBuffer] = {}
        self._last_pcm: dict[int, bytes] = {}
        self._control_cache: dict[int, dict] = {}
        self._control_loaded_at = 0.0

    def reset(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._returns.clear()
            self._last_pcm.clear()
            self._control_cache.clear()
        for session in sessions:
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(session.stop())
                except RuntimeError:
                    asyncio.run(session.stop())
            except Exception:
                pass

    def register(self, session_id: int, rtc_session) -> None:
        with self._lock:
            self._sessions[int(session_id)] = rtc_session
            self._returns.setdefault(int(session_id), GuestReturnBuffer())
            self._control_loaded_at = 0

    def invalidate_controls(self) -> None:
        with self._lock:
            self._control_loaded_at = 0

    def unregister(self, session_id: int):
        with self._lock:
            self._returns.pop(int(session_id), None)
            self._last_pcm.pop(int(session_id), None)
            self._control_cache.pop(int(session_id), None)
            return self._sessions.pop(int(session_id), None)

    def session(self, session_id: int):
        with self._lock:
            return self._sessions.get(int(session_id))

    def return_buffer(self, session_id: int) -> GuestReturnBuffer:
        with self._lock:
            return self._returns.setdefault(int(session_id), GuestReturnBuffer())

    def _controls(self) -> dict[int, dict]:
        now = time.monotonic()
        with self._lock:
            if now - self._control_loaded_at < 0.2:
                return dict(self._control_cache)
        init_db()
        conn = get_connection()
        try:
            rows = conn.execute("SELECT id, station_id, status, is_connected, is_muted, is_on_air, gain_db FROM guest_sessions WHERE status='admitted'").fetchall()
            controls = {int(row["id"]): dict(row) for row in rows}
        finally:
            conn.close()
        with self._lock:
            self._control_cache = controls
            self._control_loaded_at = now
        return dict(controls)

    def read_on_air_pcm(self, station_id: int, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested == 0:
            return b""
        controls = self._controls()
        with self._lock:
            sessions = dict(self._sessions)
        sources = []
        for session_id, session in sessions.items():
            control = controls.get(session_id)
            if not control or int(control["station_id"]) != int(station_id):
                continue
            chunk = session.read_pcm(requested)
            self._last_pcm[session_id] = chunk
            if not bool(control["is_connected"]) or bool(control["is_muted"]) or not bool(control["is_on_air"]):
                continue
            sources.append((chunk, math.pow(10.0, float(control["gain_db"] or 0.0) / 20.0)))
        if not sources:
            return b"\x00" * requested
        output = bytearray(requested)
        for offset in range(0, requested - 1, 2):
            mixed = sum(_sample(chunk, offset) * gain for chunk, gain in sources)
            output[offset : offset + 2] = _clamp(mixed).to_bytes(2, "little", signed=True)
        return bytes(output)

    def publish_program_pcm(self, station_id: int, program_stereo_pcm: bytes, *, voice_gain: float = 1.0) -> None:
        controls = self._controls()
        with self._lock:
            return_buffers = dict(self._returns)
            last_pcm = dict(self._last_pcm)
        for session_id, buffer in return_buffers.items():
            control = controls.get(session_id)
            if not control or int(control["station_id"]) != int(station_id) or not bool(control["is_connected"]):
                continue
            own = last_pcm.get(session_id, b"") if bool(control["is_on_air"]) and not bool(control["is_muted"]) else b""
            gain = math.pow(10.0, float(control["gain_db"] or 0.0) / 20.0) * max(0.0, float(voice_gain))
            if not own:
                buffer.push(program_stereo_pcm)
                continue
            output = bytearray(program_stereo_pcm)
            frames = len(program_stereo_pcm) // 4
            for frame in range(frames):
                own_sample = _sample(own, frame * 2) * gain
                for channel in (0, 2):
                    offset = frame * 4 + channel
                    value = _sample(program_stereo_pcm, offset) - own_sample
                    output[offset : offset + 2] = _clamp(value).to_bytes(2, "little", signed=True)
            buffer.push(bytes(output))

    def publish_talkback_pcm(self, station_id: int, mono_pcm: bytes) -> None:
        controls = self._controls()
        with self._lock:
            buffers = dict(self._returns)
        frames = len(mono_pcm) // 2
        stereo = bytearray(frames * 4)
        for frame in range(frames):
            value = mono_pcm[frame * 2 : frame * 2 + 2]
            stereo[frame * 4 : frame * 4 + 2] = value
            stereo[frame * 4 + 2 : frame * 4 + 4] = value
        for session_id, buffer in buffers.items():
            control = controls.get(session_id)
            if control and int(control["station_id"]) == int(station_id) and bool(control["is_connected"]):
                buffer.push(bytes(stereo))

    def snapshots(self, station_id: int) -> list[dict]:
        controls = self._controls()
        with self._lock:
            sessions = dict(self._sessions)
        output = []
        for session_id, session in sessions.items():
            control = controls.get(session_id)
            if control and int(control["station_id"]) == int(station_id):
                output.append({"session_id": session_id, **control, **session.snapshot()})
        return output

    def has_on_air(self, station_id: int) -> bool:
        return any(
            int(control["station_id"]) == int(station_id)
            and bool(control["is_connected"])
            and bool(control["is_on_air"])
            and not bool(control["is_muted"])
            for control in self._controls().values()
        )


guest_audio_registry = GuestAudioRegistry()


class GuestTalkbackRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[int, object] = {}
        self._stops: dict[int, threading.Event] = {}

    def start(self, station_id: int, input_format: str = "webm") -> dict:
        from app.audio.live_render_session import LiveRenderSession

        self.stop(station_id)
        session = LiveRenderSession(int(station_id), input_format=input_format, input_sample_rate=48000, input_channels=1)
        session.start()
        stop_event = threading.Event()
        with self._lock:
            self._sessions[int(station_id)] = session
            self._stops[int(station_id)] = stop_event

        def pump():
            while not stop_event.wait(0.02):
                chunk = session.read_pcm(1920)
                if chunk and any(chunk):
                    guest_audio_registry.publish_talkback_pcm(int(station_id), chunk)

        threading.Thread(target=pump, name=f"guest-talkback-{station_id}", daemon=True).start()
        return {"station_id": int(station_id), "active": True}

    def push(self, station_id: int, chunk: bytes) -> dict:
        with self._lock:
            session = self._sessions.get(int(station_id))
        if session is None:
            raise RuntimeError("talkback_not_started")
        session.push_chunk(bytes(chunk))
        return {"station_id": int(station_id), "active": True, "accepted_bytes": len(chunk)}

    def stop(self, station_id: int) -> dict:
        with self._lock:
            session = self._sessions.pop(int(station_id), None)
            event = self._stops.pop(int(station_id), None)
        if event:
            event.set()
        if session:
            session.stop()
        return {"station_id": int(station_id), "active": False}


guest_talkback_registry = GuestTalkbackRegistry()


try:
    from aiortc import AudioStreamTrack

    class GuestReturnAudioTrack(AudioStreamTrack):
        kind = "audio"

        def __init__(self, session_id: int):
            super().__init__()
            self.session_id = int(session_id)

        async def recv(self):
            import av

            pts, time_base = await self.next_timestamp()
            samples = 960
            pcm = guest_audio_registry.return_buffer(self.session_id).read(samples * 2 * 2)
            frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
            frame.planes[0].update(pcm)
            frame.sample_rate = 48000
            frame.pts = pts
            frame.time_base = time_base
            return frame
except ImportError:
    class GuestReturnAudioTrack:  # type: ignore[no-redef]
        def __init__(self, session_id: int):
            self.session_id = int(session_id)
