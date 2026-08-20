from datetime import datetime, timezone

EVENT_ENGINE_EVENT = "engine.event"
EVENT_HEALTH_UPDATED = "health.updated"
EVENT_QUEUE_UPDATED = "queue.updated"
EVENT_RUNTIME_UPDATED = "runtime.updated"
EVENT_TRACK_CHANGED = "track.changed"
EVENT_PONG = "pong"
EVENT_MIC_STATUS = "mic.status"
EVENT_MIC_LEVEL = "mic.level"
EVENT_MIC_ERROR = "mic.error"
EVENT_STUDIO_STATUS = "studio.status"
EVENT_DJ_PRESENCE = "dj.presence"
EVENT_CHAT_MESSAGE = "chat.message"
EVENT_WEBRTC_ANSWER = "webrtc.answer"
EVENT_WEBRTC_ICE = "webrtc.ice"
EVENT_WEBRTC_ERROR = "webrtc.error"
EVENT_SOUNDBOARD_PLAYED = "soundboard.played"
EVENT_SOUNDBOARD_STOPPED = "soundboard.stopped"
EVENT_SHOW_PREPARING = "show.preparing"
EVENT_SHOW_GOING_LIVE = "show.going_live"
EVENT_SHOW_INTRO_PLAYING = "show.intro_playing"
EVENT_SHOW_LIVE = "show.live"
EVENT_SHOW_BREAK_START = "show.break_start"
EVENT_SHOW_BREAK_END = "show.break_end"
EVENT_SHOW_OUTRO_PLAYING = "show.outro_playing"
EVENT_SHOW_ENDED = "show.ended"
EVENT_SHOW_QUEUE_LOW = "show.queue_low"
EVENT_AD_BREAK_UPCOMING = "ad_break.upcoming"
EVENT_AD_BREAK_MISSED = "ad_break.missed"


def make_event(event_type: str, station_id: int, payload: dict | None = None) -> dict:
    return {
        "type": str(event_type or "").strip(),
        "station_id": int(station_id),
        "payload": dict(payload or {}),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
