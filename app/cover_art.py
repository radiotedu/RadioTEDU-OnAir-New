from __future__ import annotations

from urllib.parse import quote


def public_track_cover_url(station_id: int, cover_art_url: str = "") -> str:
    """Return a browser-safe cover-art URL for stored track metadata."""
    raw = str(cover_art_url or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "/")):
        return raw
    name = raw.replace("\\", "/").split("/")[-1]
    if not name:
        return ""
    return f"/api/media/{int(station_id)}/cover-art/{quote(name)}"
