import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import get_max_upload_bytes

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_AUDIO_EXTENSIONS = {
    ".aac",
    ".adts",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
}
_UPLOAD_CHUNK_SIZE = 1024 * 1024


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_under_root(root: Path, relative_path: str) -> Path | None:
    text = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if not text:
        return None
    full = (root.resolve() / text).resolve()
    if not is_within_root(full, root):
        return None
    return full


def sanitize_upload_filename(
    filename: str,
    *,
    default_stem: str = "upload",
    default_extension: str = ".bin",
    allowed_extensions: set[str] | None = None,
) -> str:
    raw_name = Path(str(filename or "")).name
    stem = Path(raw_name).stem.strip()
    suffix = Path(raw_name).suffix.lower()
    if not stem:
        stem = str(default_stem or "upload")
    safe_stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._-") or str(default_stem or "upload")
    safe_suffix = suffix or str(default_extension or ".bin").lower()
    if not safe_suffix.startswith("."):
        safe_suffix = f".{safe_suffix}"
    if allowed_extensions is not None and safe_suffix.lower() not in {
        str(ext).lower() for ext in allowed_extensions
    }:
        raise HTTPException(status_code=400, detail="unsupported_file_type")
    return f"{safe_stem}{safe_suffix.lower()}"


def reserve_unique_path(target_dir: Path, filename: str) -> Path:
    safe_name = Path(str(filename or "upload.bin")).name
    stem = Path(safe_name).stem or "upload"
    suffix = Path(safe_name).suffix or ".bin"
    destination = target_dir / safe_name
    counter = 1
    while destination.exists():
        destination = target_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return destination


async def write_upload_to_path(
    upload: UploadFile,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> int:
    limit = max(1, int(max_bytes or get_max_upload_bytes()))
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail="upload_too_large")
                handle.write(chunk)
        return total
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except Exception:
            pass
        raise


async def save_upload_file(
    upload: UploadFile,
    target_dir: Path,
    *,
    default_stem: str = "upload",
    default_extension: str = ".bin",
    allowed_extensions: set[str] | None = None,
    max_bytes: int | None = None,
) -> Path:
    safe_name = sanitize_upload_filename(
        str(upload.filename or ""),
        default_stem=default_stem,
        default_extension=default_extension,
        allowed_extensions=allowed_extensions,
    )
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = reserve_unique_path(target_dir, safe_name)
    await write_upload_to_path(upload, destination, max_bytes=max_bytes)
    return destination


def audio_upload_extensions() -> set[str]:
    return set(_AUDIO_EXTENSIONS)


def safe_unlink(path: str | Path, *, root: Path) -> bool:
    candidate = Path(path).resolve()
    if not is_within_root(candidate, root):
        return False
    try:
        candidate.unlink(missing_ok=True)
        return True
    except Exception:
        return False
