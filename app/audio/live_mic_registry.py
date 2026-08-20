import asyncio
import threading

from app.audio.live_render_session import LiveRenderSession
from app.audio.mic_session import MicSession

_DEFAULT_STATUS = {
    "live_input_enabled": False,
    "transmitting": False,
    "active_user": None,
    "receiving": False,
    "level_db": -60.0,
    "peak_db": -60.0,
    "buffer_bytes": 0,
    "last_error": "",
}


class LiveMicRegistry:
    def __init__(self, session_factory=None, render_session_factory=None):
        self._session_factory = session_factory or (lambda station_id: MicSession(station_id))
        self._render_session_factory = render_session_factory or (
            lambda station_id, **kwargs: LiveRenderSession(station_id, **kwargs)
        )
        self._lock = threading.Lock()
        self._stations: dict[int, dict] = {}
        self._listeners: list = []

    def register_listener(self, listener) -> None:
        if listener is None:
            return
        self._listeners.append(listener)

    def _notify_listeners(self, event_type: str, station_id: int, snapshot: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(str(event_type), int(station_id), dict(snapshot))
            except Exception:
                continue

    def reset(self) -> None:
        with self._lock:
            stations = list(self._stations.values())
            self._stations = {}
        for state in stations:
            session = state.get("session")
            if session is not None:
                try:
                    session.stop()
                except Exception:
                    pass
            rtc_session = state.get("rtc_session")
            if rtc_session is not None:
                self._stop_rtc(rtc_session)
            render_session = state.get("render_session")
            if render_session is not None:
                try:
                    render_session.stop()
                except Exception:
                    pass

    def _stop_rtc(self, rtc_session) -> None:
        """Schedule an async stop() call for an RTC session, best-effort."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(rtc_session.stop())
        except RuntimeError:
            try:
                asyncio.run(rtc_session.stop())
            except Exception:
                pass
        except Exception:
            pass

    def _state_for(self, station_id: int) -> dict:
        sid = int(station_id)
        state = self._stations.get(sid)
        if state is None:
            state = {
                "session": None,
                "rtc_session": None,
                "render_session": None,
                "transmitting": False,
                "active_user": None,
                "source_name": "",
            }
            self._stations[sid] = state
        return state

    def _ensure_session(self, station_id: int):
        with self._lock:
            state = self._state_for(station_id)
            session = state.get("session")
            if session is None:
                session = self._session_factory(int(station_id))
                state["session"] = session
        if session is None:
            raise RuntimeError("mic session unavailable")
        if not session.running:
            session.start()
        return session

    def start_transmission(self, station_id: int, user: dict) -> dict:
        old_rtc = None
        old_render = None
        with self._lock:
            state = self._state_for(station_id)
            old_rtc = state.get("rtc_session")
            old_render = state.get("render_session")
            state["rtc_session"] = None
            state["render_session"] = None
            state["transmitting"] = True
            state["active_user"] = {
                "id": int(user["id"]),
                "username": str(user["username"]),
                "role": str(user["role"]),
            }
            state["source_name"] = ""
        if old_rtc is not None:
            self._stop_rtc(old_rtc)
        if old_render is not None:
            try:
                old_render.stop()
            except Exception:
                pass
        snapshot = self.snapshot(station_id)
        self._notify_listeners("start", station_id, snapshot)
        return snapshot

    def stop_transmission(self, station_id: int, user_id: int | None = None) -> dict:
        with self._lock:
            state = self._state_for(station_id)
            active_user = state.get("active_user")
            if user_id is None or active_user is None or int(active_user.get("id", 0)) == int(user_id):
                state["transmitting"] = False
                state["active_user"] = None
                state["source_name"] = ""
        snapshot = self.snapshot(station_id)
        self._notify_listeners("stop", station_id, snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # WebRTC session management
    # ------------------------------------------------------------------

    def start_rtc_session(self, station_id: int, rtc_session, user: dict) -> dict:
        """Register a WebRTC mic session, replacing any existing WS or RTC session."""
        sid = int(station_id)
        old_ws = None
        old_rtc = None
        old_render = None
        with self._lock:
            state = self._state_for(sid)
            old_ws = state.get("session")
            old_rtc = state.get("rtc_session")
            old_render = state.get("render_session")
            state["session"] = None
            state["rtc_session"] = rtc_session
            state["render_session"] = None
            state["transmitting"] = True
            state["active_user"] = {
                "id": int(user["id"]),
                "username": str(user["username"]),
                "role": str(user["role"]),
            }
            state["source_name"] = ""

        if old_ws is not None:
            try:
                old_ws.stop()
            except Exception:
                pass
        if old_rtc is not None:
            self._stop_rtc(old_rtc)
        if old_render is not None:
            try:
                old_render.stop()
            except Exception:
                pass

        snapshot = self.snapshot(sid)
        self._notify_listeners("start", sid, snapshot)
        return snapshot

    def stop_rtc_session(self, station_id: int, user_id: int | None = None) -> dict:
        """Clear the WebRTC session for a station."""
        sid = int(station_id)
        old_rtc = None
        with self._lock:
            state = self._state_for(sid)
            active_user = state.get("active_user")
            if user_id is None or active_user is None or int(active_user.get("id", 0)) == int(user_id):
                old_rtc = state.get("rtc_session")
                state["rtc_session"] = None
                state["transmitting"] = False
                state["active_user"] = None
                state["source_name"] = ""

        if old_rtc is not None:
            self._stop_rtc(old_rtc)

        snapshot = self.snapshot(sid)
        self._notify_listeners("stop", sid, snapshot)
        return snapshot

    def get_rtc_session(self, station_id: int):
        """Return the active RtcMicSession for station_id, or None."""
        sid = int(station_id)
        with self._lock:
            state = self._stations.get(sid)
            if state is None:
                return None
            return state.get("rtc_session")

    # ------------------------------------------------------------------
    # External render session management
    # ------------------------------------------------------------------

    def start_render_transmission(
        self,
        station_id: int,
        user: dict,
        *,
        source_name: str = "OmniVoice",
        input_format: str = "s16le",
        sample_rate: int = 24000,
        channels: int = 1,
        max_buffer_bytes: int = 480000,
    ) -> dict:
        sid = int(station_id)
        render_session = self._render_session_factory(
            sid,
            input_format=str(input_format or "s16le"),
            input_sample_rate=int(sample_rate or 24000),
            input_channels=int(channels or 1),
            max_buffer_bytes=int(max_buffer_bytes or 480000),
        )
        if not render_session.running:
            render_session.start()

        old_ws = None
        old_rtc = None
        old_render = None
        with self._lock:
            state = self._state_for(sid)
            old_ws = state.get("session")
            old_rtc = state.get("rtc_session")
            old_render = state.get("render_session")
            state["session"] = None
            state["rtc_session"] = None
            state["render_session"] = render_session
            state["transmitting"] = True
            state["active_user"] = {
                "id": int(user["id"]),
                "username": str(user["username"]),
                "role": str(user["role"]),
            }
            state["source_name"] = str(source_name or "OmniVoice").strip() or "OmniVoice"

        if old_ws is not None:
            try:
                old_ws.stop()
            except Exception:
                pass
        if old_rtc is not None:
            self._stop_rtc(old_rtc)
        if old_render is not None and old_render is not render_session:
            try:
                old_render.stop()
            except Exception:
                pass

        snapshot = self.snapshot(sid)
        self._notify_listeners("start", sid, snapshot)
        return snapshot

    def push_render_chunk(self, station_id: int, chunk: bytes) -> dict:
        with self._lock:
            state = self._state_for(station_id)
            render_session = state.get("render_session")
        if render_session is None or not render_session.running:
            raise RuntimeError("render session is not running")
        render_session.push_chunk(chunk)
        return self.snapshot(station_id)

    def stop_render_transmission(self, station_id: int, user_id: int | None = None) -> dict:
        sid = int(station_id)
        render_session = None
        with self._lock:
            state = self._state_for(sid)
            active_user = state.get("active_user")
            if user_id is not None and active_user is not None:
                if int(active_user.get("id", 0)) != int(user_id):
                    return self.snapshot(sid)
            render_session = state.get("render_session")
            state["render_session"] = None
            state["transmitting"] = False
            state["active_user"] = None
            state["source_name"] = ""
        if render_session is not None:
            try:
                render_session.stop()
            except Exception:
                pass
        snapshot = self.snapshot(sid)
        self._notify_listeners("stop", sid, snapshot)
        return snapshot

    def stop_live_input(self, station_id: int, user_id: int | None = None) -> dict:
        sid = int(station_id)
        session = None
        rtc_session = None
        render_session = None
        with self._lock:
            state = self._state_for(sid)
            active_user = state.get("active_user")
            if user_id is not None and active_user is not None:
                if int(active_user.get("id", 0)) != int(user_id):
                    return self.snapshot(sid)
            session = state.get("session")
            rtc_session = state.get("rtc_session")
            render_session = state.get("render_session")
            state["session"] = None
            state["rtc_session"] = None
            state["render_session"] = None
            state["transmitting"] = False
            state["active_user"] = None
            state["source_name"] = ""

        if session is not None:
            try:
                session.stop()
            except Exception:
                pass
        if rtc_session is not None:
            self._stop_rtc(rtc_session)
        if render_session is not None:
            try:
                render_session.stop()
            except Exception:
                pass

        snapshot = self.snapshot(sid)
        self._notify_listeners("stop", sid, snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # Audio I/O
    # ------------------------------------------------------------------

    def push_chunk(self, station_id: int, chunk: bytes) -> dict:
        # No-op when another active source is already driving PCM delivery.
        with self._lock:
            state = self._state_for(station_id)
            rtc_session = state.get("rtc_session")
            render_session = state.get("render_session")
        if rtc_session is not None and rtc_session.running:
            return self.snapshot(station_id)
        if render_session is not None and render_session.running:
            return self.snapshot(station_id)
        session = self._ensure_session(station_id)
        session.push_chunk(chunk)
        return self.snapshot(station_id)

    def read_pcm(self, station_id: int, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested <= 0:
            return b""
        with self._lock:
            state = self._state_for(station_id)
            render_session = state.get("render_session")
            rtc_session = state.get("rtc_session")
            session = state.get("session")
        if render_session is not None and render_session.running:
            return render_session.read_pcm(requested)
        if rtc_session is not None and rtc_session.running:
            return rtc_session.read_pcm(requested)
        if session is None:
            return b"\x00" * requested
        return session.read_pcm(requested)

    def snapshot(self, station_id: int) -> dict:
        sid = int(station_id)
        with self._lock:
            state = self._state_for(sid)
            session = state.get("session")
            render_session = state.get("render_session")
            rtc_session = state.get("rtc_session")
            render_active = render_session is not None and render_session.running
            rtc_active = rtc_session is not None and rtc_session.running
            if render_active:
                transport = "render"
            elif rtc_active:
                transport = "webrtc"
            else:
                transport = "websocket"
            payload = {
                "station_id": sid,
                **_DEFAULT_STATUS,
                "live_input_enabled": bool(
                    state.get("transmitting") or session is not None or rtc_active or render_active
                ),
                "transmitting": bool(state.get("transmitting")),
                "active_user": state.get("active_user"),
                "transport": transport,
                "source_name": str(state.get("source_name") or ""),
            }

        if render_active:
            render_snapshot = render_session.snapshot()
            payload.update(
                {
                    "receiving": bool(render_snapshot.get("receiving")),
                    "level_db": float(render_snapshot.get("level_db", -60.0)),
                    "peak_db": float(render_snapshot.get("peak_db", -60.0)),
                    "buffer_bytes": int(render_snapshot.get("buffer_bytes", 0)),
                    "last_error": str(render_snapshot.get("last_error", "")),
                }
            )
        elif rtc_active:
            rtc_snapshot = rtc_session.snapshot()
            payload.update(
                {
                    "receiving": bool(rtc_snapshot.get("receiving")),
                    "level_db": float(rtc_snapshot.get("level_db", -60.0)),
                    "peak_db": float(rtc_snapshot.get("peak_db", -60.0)),
                    "buffer_bytes": int(rtc_snapshot.get("buffer_bytes", 0)),
                    "last_error": str(rtc_snapshot.get("last_error", "")),
                }
            )
        elif session is not None:
            session_snapshot = session.snapshot()
            payload.update(
                {
                    "receiving": bool(session_snapshot.get("receiving")),
                    "level_db": float(session_snapshot.get("level_db", -60.0)),
                    "peak_db": float(session_snapshot.get("peak_db", -60.0)),
                    "buffer_bytes": int(session_snapshot.get("buffer_bytes", 0)),
                    "last_error": str(session_snapshot.get("last_error", "")),
                }
            )
        return payload


live_mic_registry = LiveMicRegistry()
