from pathlib import Path

from app.config import get_db_path
from app.file_security import resolve_under_root


def _uploads_root() -> Path:
    return (get_db_path().parent / "uploads").resolve()


def _legacy_uploads_relative_path(raw_path: str) -> Path | None:
    normalized = str(raw_path or "").strip().replace("\\", "/")
    if not normalized:
        return None
    lower = normalized.lower()
    for marker in ("/data/uploads/", "/uploads/"):
        index = lower.find(marker)
        if index < 0:
            continue
        relative = normalized[index + len(marker) :].strip("/")
        if relative:
            return Path(relative)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        return candidate
    return None


def resolve_runtime_media_path(raw_path: str) -> str:
    text = str(raw_path or "").strip()
    if not text or "://" in text:
        return text

    candidate = Path(text)
    if candidate.exists():
        return str(candidate.resolve())

    relative = _legacy_uploads_relative_path(text)
    if relative is None:
        return text

    remapped = resolve_under_root(_uploads_root(), str(relative))
    if remapped is None:
        return text
    if remapped.exists():
        return str(remapped)
    return text
