from __future__ import annotations

import os
import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from app.file_security import sanitize_upload_filename, write_upload_to_path
from app.runtime_paths import resolve_binary


SUPPORTED_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"}
)
TRASH_DIR_NAME = ".radiotedu-trash"
MAX_LIST_LIMIT = 500
_LOCK = threading.RLock()


def _read_env(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=409, detail="juke_config_unreadable") from exc
    values: dict[str, str] = {}
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#") or "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def configured_roots(config_path: str | Path) -> list[dict[str, Any]]:
    path = Path(str(config_path or "")).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=409, detail="juke_config_not_ready")
    values = _read_env(path)
    candidates = (
        ("primary", "Primary music library", values.get("LOCAL_MUSIC_ROOT", "")),
        (
            "overflow",
            "Overflow music library",
            values.get("LOCAL_MUSIC_OVERFLOW_ROOT", ""),
        ),
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root_id, label, raw in candidates:
        raw = str(raw or "").strip()
        if not raw:
            continue
        root = Path(raw).expanduser().resolve(strict=False)
        identity = os.path.normcase(str(root))
        if identity in seen:
            continue
        seen.add(identity)
        output.append(
            {
                "id": root_id,
                "label": label,
                "path": str(root),
                "ready": root.is_dir(),
                "writable": root.is_dir() and os.access(root, os.W_OK),
            }
        )
    if not output:
        raise HTTPException(status_code=409, detail="juke_library_roots_not_configured")
    return output


def _root_path(config_path: str | Path, root_id: str) -> Path:
    token = str(root_id or "").strip().lower()
    for item in configured_roots(config_path):
        if item["id"] == token:
            root = Path(item["path"])
            if not root.is_dir():
                raise HTTPException(status_code=409, detail="juke_library_root_not_ready")
            return root.resolve()
    raise HTTPException(status_code=404, detail="unknown_juke_library_root")


def _relative_path(raw: str, *, allow_empty: bool = False) -> Path:
    token = str(raw or "").strip().replace("\\", "/")
    if not token and allow_empty:
        return Path()
    pure = PurePosixPath(token)
    if (
        not token
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or ":" in pure.parts[0]
    ):
        raise HTTPException(status_code=400, detail="invalid_juke_relative_path")
    return Path(*pure.parts)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _media_item(root_id: str, root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    return {
        "root_id": root_id,
        "relative_path": relative,
        "name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": int(stat.st_size),
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def _validate_audio(path: Path) -> dict[str, Any]:
    ffprobe = resolve_binary("ffprobe.exe") or resolve_binary("ffprobe")
    if not ffprobe:
        raise HTTPException(status_code=409, detail="juke_audio_probe_unavailable")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=400, detail="juke_audio_probe_failed") from exc
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError):
        payload = {}
    streams = payload.get("streams") if isinstance(payload, dict) else []
    stream = streams[0] if isinstance(streams, list) and streams else {}
    if result.returncode != 0 or not str(stream.get("codec_name") or "").strip():
        raise HTTPException(status_code=400, detail="invalid_juke_audio_file")
    try:
        duration = max(0.0, float((payload.get("format") or {}).get("duration") or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "codec": str(stream.get("codec_name") or ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "duration_seconds": round(duration, 3),
    }


def _walk_media(root: Path, *, include_trash: bool) -> list[Path]:
    start = root / TRASH_DIR_NAME if include_trash else root
    if not start.is_dir():
        return []
    items: list[Path] = []
    for current, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(
            [
                name
                for name in dirnames
                if not Path(current, name).is_symlink()
                and (include_trash or name != TRASH_DIR_NAME)
            ],
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            path = Path(current, filename)
            if path.is_symlink() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            items.append(path)
    return items


def list_library(
    config_path: str | Path,
    *,
    query: str = "",
    root_id: str = "",
    include_trash: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    selected_root = str(root_id or "").strip().lower()
    search = str(query or "").strip().casefold()
    safe_limit = max(1, min(MAX_LIST_LIMIT, int(limit or 200)))
    roots = configured_roots(config_path)
    if selected_root:
        roots = [item for item in roots if item["id"] == selected_root]
        if not roots:
            raise HTTPException(status_code=404, detail="unknown_juke_library_root")
    rows: list[dict[str, Any]] = []
    per_root_count: dict[str, int] = {}
    skipped_unreadable = 0
    for root_info in roots:
        if not root_info["ready"]:
            per_root_count[root_info["id"]] = 0
            continue
        root = Path(root_info["path"]).resolve()
        paths = _walk_media(root, include_trash=include_trash)
        per_root_count[root_info["id"]] = 0
        for path in paths:
            try:
                item = _media_item(root_info["id"], root, path)
            except OSError:
                # Libraries are live: a file can disappear between directory
                # enumeration and stat, or be inaccessible/too long for a
                # particular Windows API. One bad item must never take down
                # the operator library.
                skipped_unreadable += 1
                continue
            per_root_count[root_info["id"]] += 1
            if include_trash:
                trash_prefix = f"{TRASH_DIR_NAME}/"
                trash_relative = item["relative_path"]
                item["trash_path"] = trash_relative
                remainder = trash_relative[len(trash_prefix) :] if trash_relative.startswith(trash_prefix) else trash_relative
                parts = remainder.split("/", 1)
                item["original_relative_path"] = parts[1] if len(parts) == 2 else item["name"]
            haystack = f"{item['relative_path']} {item['name']}".casefold()
            if search and search not in haystack:
                continue
            rows.append(item)
    root_order = {item["id"]: index for index, item in enumerate(roots)}
    rows.sort(
        key=lambda item: (
            root_order.get(str(item["root_id"]), len(root_order)),
            str(item["relative_path"]).casefold(),
            str(item["relative_path"]),
        )
    )
    return {
        "roots": configured_roots(config_path),
        "items": rows[:safe_limit],
        "matched_count": len(rows),
        "returned_count": min(len(rows), safe_limit),
        "root_counts": per_root_count,
        "include_trash": bool(include_trash),
        "skipped_unreadable": skipped_unreadable,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "deterministic_order": "configured root order, relative path case-insensitive, relative path",
    }


async def store_upload(
    config_path: str | Path,
    *,
    root_id: str,
    relative_folder: str,
    upload: UploadFile,
) -> dict[str, Any]:
    root = _root_path(config_path, root_id)
    folder = _relative_path(relative_folder, allow_empty=True)
    target_dir = (root / folder).resolve(strict=False)
    if not _inside(root, target_dir) or TRASH_DIR_NAME in target_dir.parts:
        raise HTTPException(status_code=400, detail="invalid_juke_upload_folder")
    safe_name = sanitize_upload_filename(
        str(upload.filename or ""),
        default_stem="juke-song",
        default_extension=".mp3",
        allowed_extensions=set(SUPPORTED_EXTENSIONS),
    )
    destination = target_dir / safe_name
    temporary = target_dir / f".{safe_name}.upload-{uuid4().hex}.tmp"
    reservation = target_dir / f".{safe_name}.radiotedu-upload.lock"
    target_dir.mkdir(parents=True, exist_ok=True)
    reservation_fd: int | None = None
    try:
        try:
            reservation_fd = os.open(
                reservation,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="juke_song_upload_in_progress") from exc
        if destination.exists():
            raise HTTPException(status_code=409, detail="juke_song_already_exists")
        await write_upload_to_path(upload, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        audio = _validate_audio(temporary)
        if destination.exists():
            raise HTTPException(status_code=409, detail="juke_song_already_exists")
        os.replace(temporary, destination)
    finally:
        if reservation_fd is not None:
            os.close(reservation_fd)
        reservation.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    return {
        **_media_item(str(root_id).strip().lower(), root, destination),
        "audio": audio,
    }


def retire_item(
    config_path: str | Path,
    *,
    root_id: str,
    relative_path: str,
) -> dict[str, Any]:
    root = _root_path(config_path, root_id)
    relative = _relative_path(relative_path)
    source = (root / relative).resolve(strict=False)
    if not _inside(root, source) or TRASH_DIR_NAME in source.parts:
        raise HTTPException(status_code=400, detail="invalid_juke_song_path")
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="juke_song_not_found")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / TRASH_DIR_NAME / stamp / relative
    with _LOCK:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise HTTPException(status_code=409, detail="juke_trash_collision")
        os.replace(source, destination)
    return {
        "ok": True,
        "root_id": str(root_id).strip().lower(),
        "relative_path": relative.as_posix(),
        "trash_path": destination.relative_to(root).as_posix(),
        "recoverable": True,
    }


def restore_item(
    config_path: str | Path,
    *,
    root_id: str,
    trash_path: str,
) -> dict[str, Any]:
    root = _root_path(config_path, root_id)
    relative = _relative_path(trash_path)
    if not relative.parts or relative.parts[0] != TRASH_DIR_NAME or len(relative.parts) < 3:
        raise HTTPException(status_code=400, detail="invalid_juke_trash_path")
    source = (root / relative).resolve(strict=False)
    if not _inside(root / TRASH_DIR_NAME, source) or not source.is_file():
        raise HTTPException(status_code=404, detail="juke_trashed_song_not_found")
    original = Path(*relative.parts[2:])
    destination = (root / original).resolve(strict=False)
    if not _inside(root, destination) or TRASH_DIR_NAME in destination.parts:
        raise HTTPException(status_code=400, detail="invalid_juke_restore_path")
    with _LOCK:
        if destination.exists():
            raise HTTPException(status_code=409, detail="juke_restore_target_exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        parent = source.parent
        trash_root = root / TRASH_DIR_NAME
        while parent != trash_root and _inside(trash_root, parent):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return {
        "ok": True,
        "root_id": str(root_id).strip().lower(),
        "relative_path": original.as_posix(),
        "restored": True,
    }
