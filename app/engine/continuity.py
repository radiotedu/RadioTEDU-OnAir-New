from app.audio.virtual_sources import DEFAULT_CONTINUITY_INPUT_URI
from app.media_paths import resolve_runtime_media_path
from app.repositories.settings_repo import SettingsRepository

_FALLBACK_SETTING_KEYS = (
    "continuity_fallback_uri",
    "fallback_uri",
)


def _normalize_fallback_uri(raw: str) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    return str(resolve_runtime_media_path(token) or "").strip()


def resolve_station_fallback_uri(station_id: int, conn, requested: str = "") -> str:
    explicit = _normalize_fallback_uri(requested)
    if explicit:
        return explicit

    repo = SettingsRepository(conn)
    for settings in (repo.get_station(int(station_id)), repo.get_system()):
        for key in _FALLBACK_SETTING_KEYS:
            candidate = _normalize_fallback_uri((settings or {}).get(key, ""))
            if candidate:
                return candidate

    return DEFAULT_CONTINUITY_INPUT_URI
