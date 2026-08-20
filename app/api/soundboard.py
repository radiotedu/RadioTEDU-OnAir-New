import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form

from app.auth.dependencies import require_permission
from app.db import get_connection, init_db
from app.file_security import (
    audio_upload_extensions,
    safe_unlink,
    sanitize_upload_filename,
    write_upload_to_path,
)
from app.repositories.soundboard_repo import SoundboardRepository
from app.config import get_db_path

router = APIRouter(prefix="/api/soundboard", tags=["soundboard"])


def _request_user(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
    }


@router.get("/")
def list_items(station_id: int, request: Request):
    _request_user(request)
    init_db()
    conn = get_connection()
    try:
        return SoundboardRepository(conn).list_by_station(station_id)
    finally:
        conn.close()


@router.post("/")
def create_item(
    request: Request,
    body: dict,
    _user=Depends(require_permission("soundboard.manage")),
):
    init_db()
    conn = get_connection()
    try:
        repo = SoundboardRepository(conn)
        station_id = int(body.get("station_id", 1))
        name = str(body.get("name", ""))
        file_path = str(body.get("file_path", ""))
        if not name or not file_path:
            raise HTTPException(status_code=400, detail="name and file_path required")
        kwargs = {}
        for key in ("color", "hotkey", "category", "duration_s", "gain_db", "sort_order", "uploaded"):
            if key in body:
                kwargs[key] = body[key]
        item_id = repo.create(station_id, name, file_path, **kwargs)
        return repo.get(item_id)
    finally:
        conn.close()


@router.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    station_id: int = Form(1),
    _user=Depends(require_permission("soundboard.manage")),
):
    target_dir = get_db_path().parent / "soundboard" / str(station_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    original_name = file.filename or "effect.mp3"
    safe_tail = sanitize_upload_filename(
        original_name,
        default_stem="effect",
        default_extension=".mp3",
        allowed_extensions=audio_upload_extensions(),
    )
    safe_name = f"{uuid.uuid4().hex}_{safe_tail}"
    target_path = target_dir / safe_name
    await write_upload_to_path(file, target_path)
    duration_s = _probe_duration(str(target_path))
    return {
        "file_path": str(target_path.resolve()),
        "duration_s": duration_s,
        "original_name": original_name,
    }


def _probe_duration(file_path: str) -> float | None:
    import subprocess
    from app.runtime_paths import resolve_binary

    ffprobe = resolve_binary("ffprobe.exe") or resolve_binary("ffprobe") or "ffprobe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return round(float(result.stdout.strip()), 2)
    except Exception:
        return None


@router.post("/play")
async def play_item(request: Request, body: dict):
    user = _request_user(request)
    item_id = int(body.get("item_id", 0))
    station_id = int(body.get("station_id", 1))
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="item_id required")
    init_db()
    conn = get_connection()
    try:
        repo = SoundboardRepository(conn)
        item = repo.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        if not os.path.isfile(item["file_path"]):
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
    finally:
        conn.close()
    from app.api.runtime import runtime_registry
    player = runtime_registry.get_sound_effect_player(station_id)
    if player is None:
        raise HTTPException(status_code=409, detail="Station runtime not running")
    player.play(item)
    from app.ws.broadcaster import broadcaster
    from app.ws.events import EVENT_SOUNDBOARD_PLAYED
    await broadcaster.broadcast_soundboard_event(station_id, EVENT_SOUNDBOARD_PLAYED, {
        "item_id": item["id"],
        "name": item["name"],
        "duration_s": item.get("duration_s"),
        "played_by": user["username"],
    })
    return {"playing": True, "item_id": item["id"], "name": item["name"]}


@router.post("/stop")
async def stop_item(request: Request, body: dict):
    _request_user(request)
    station_id = int(body.get("station_id", 1))
    item_id = body.get("item_id")
    from app.api.runtime import runtime_registry
    player = runtime_registry.get_sound_effect_player(station_id)
    if player is None:
        raise HTTPException(status_code=409, detail="Station runtime not running")
    player.stop(item_id=int(item_id) if item_id is not None else None)
    from app.ws.broadcaster import broadcaster
    from app.ws.events import EVENT_SOUNDBOARD_STOPPED
    await broadcaster.broadcast_soundboard_event(station_id, EVENT_SOUNDBOARD_STOPPED, {
        "item_id": int(item_id) if item_id is not None else None,
        "stopped_all": item_id is None,
    })
    return {"stopped": True}


@router.put("/{item_id}")
def update_item(
    item_id: int,
    request: Request,
    body: dict,
    _user=Depends(require_permission("soundboard.manage")),
):
    init_db()
    conn = get_connection()
    try:
        repo = SoundboardRepository(conn)
        item = repo.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        updated = repo.update(item_id, **body)
        return updated
    finally:
        conn.close()


@router.delete("/{item_id}", status_code=204)
def delete_item(
    item_id: int,
    request: Request,
    _user=Depends(require_permission("soundboard.manage")),
):
    init_db()
    conn = get_connection()
    try:
        repo = SoundboardRepository(conn)
        item = repo.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        # Stop if currently playing
        from app.api.runtime import runtime_registry
        player = runtime_registry.get_sound_effect_player(item["station_id"])
        if player is not None:
            player.stop(item_id=item_id)
        # Delete uploaded file
        if item.get("uploaded") == 1:
            safe_unlink(
                str(item["file_path"] or ""),
                root=(get_db_path().parent / "soundboard"),
            )
        repo.delete(item_id)
    finally:
        conn.close()
