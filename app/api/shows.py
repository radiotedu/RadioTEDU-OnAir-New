import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.auth.dependencies import (
    get_current_user,
    require_permission,
    require_show_permission,
    user_has_permission,
    user_has_show_permission,
)
from app.auth.permissions import SHOW_PERMISSION_KEYS
from app.config import get_db_path
from app.db import get_connection, init_db
from app.file_security import (
    audio_upload_extensions,
    sanitize_upload_filename,
    safe_unlink,
    write_upload_to_path,
)
from app.repositories.rbac_repo import RbacRepository
from app.repositories.show_repo import ShowRepository
from app.repositories.show_session_repo import ShowSessionRepository
from app.repositories.program_queue_repo import ProgramQueueRepository
from app.repositories.settings_repo import SettingsRepository
from app.repositories.user_repo import UserRepository

router = APIRouter(prefix="/api/shows", tags=["shows"])


class ShowCreatePayload(BaseModel):
    station_id: int = 1
    name: str
    description: str = ""
    color: str = "#4a90d9"


class ShowUpdatePayload(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    is_active: int | None = None


class AssignPayload(BaseModel):
    user_id: int
    role: str = "dj"
    permission_keys: list[str] | None = None


class GoLivePayload(BaseModel):
    station_id: int = 1


def _request_user(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
    }


def _require_admin(user: dict) -> None:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


def _show_to_dict(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "station_id": int(row["station_id"]),
        "name": str(row["name"]),
        "description": str(row.get("description") or ""),
        "color": str(row.get("color") or "#4a90d9"),
        "intro_path": row.get("intro_path"),
        "outro_path": row.get("outro_path"),
        "break_outro_path": row.get("break_outro_path"),
        "break_intro_path": row.get("break_intro_path"),
        "is_active": int(row.get("is_active", 1)),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _broadcast_show_ws(event_type: str, station_id: int, payload: dict) -> None:
    try:
        from app.ws.broadcaster import broadcaster
        broadcaster.on_show_event(int(station_id), event_type, payload)
    except Exception:
        pass


def _log_show_transition(conn, station_id: int, show_id: int, from_status: str, to_status: str) -> None:
    """Log show state transitions to operation_logs for analytics."""
    try:
        from app.repositories.log_repo import LogRepository
        LogRepository(conn).add_operation_log(
            station_id=station_id,
            message=f"Show {show_id}: {from_status} → {to_status}",
            event_type="show.transition",
            payload={"show_id": show_id, "from_status": from_status, "to_status": to_status},
        )
    except Exception:
        pass


def _require_show_role(user: dict, show_id: int, conn, *, allowed_roles: set) -> dict:
    """Verify user is admin or has one of allowed_roles assigned to this show."""
    if user["role"] == "admin":
        return user
    repo = ShowRepository(conn)
    if not repo.is_assigned(show_id, user["id"]):
        raise HTTPException(status_code=403, detail="Not assigned to this show")
    assignments = repo.list_assignments(show_id)
    user_assignment = next((a for a in assignments if int(a["user_id"]) == user["id"]), None)
    if not user_assignment or user_assignment["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail=f"Requires one of: {', '.join(sorted(allowed_roles))}")
    return user


def _normalize_permission_keys(permission_keys: list[str]) -> set[str]:
    normalized = {
        str(permission_key).strip()
        for permission_key in (permission_keys or [])
        if str(permission_key).strip()
    }
    invalid = sorted(
        permission
        for permission in normalized
        if permission not in SHOW_PERMISSION_KEYS
    )
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid permission key: {invalid[0]}",
        )
    return normalized


def _assignment_permission_keys(row: dict, rbac_repo: RbacRepository, show_id: int) -> set[str]:
    raw_permission_keys = str(row.get("permission_keys_json") or "")
    if raw_permission_keys:
        try:
            parsed = json.loads(raw_permission_keys)
        except json.JSONDecodeError:
            parsed = []
        return {
            str(permission).strip()
            for permission in parsed
            if str(permission).strip()
        }
    return rbac_repo.list_show_permissions(show_id, int(row["user_id"]))


def _serialize_assignment(row: dict, permission_keys: set[str]) -> dict:
    data = dict(row)
    data.pop("permission_keys_json", None)
    data["permission_keys"] = sorted(str(permission) for permission in permission_keys)
    return data


def _serialize_assignments(repo: ShowRepository, rbac_repo: RbacRepository, show_id: int) -> list[dict]:
    return [
        _serialize_assignment(row, _assignment_permission_keys(row, rbac_repo, show_id))
        for row in repo.list_assignments(show_id)
    ]


def _serialize_assignment_candidate(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "display_name": str(row["display_name"]),
        "role": str(row["role"]),
        "is_active": bool(row["is_active"]),
    }


_PROGRAM_WORKSPACE_SHOW_KEY = "program_workspace_show_id"


def _get_program_workspace_claim_show_id(conn, station_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM station_settings WHERE station_id=? AND key=?",
        (int(station_id), _PROGRAM_WORKSPACE_SHOW_KEY),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    try:
        return int(str(row["value"] or "0").strip() or "0")
    except ValueError:
        return 0


def _require_program_workspace_claim_for_show(conn, station_id: int, show_id: int) -> None:
    active_session = ShowSessionRepository(conn).get_active_for_station(int(station_id))
    if active_session is not None:
        if int(active_session["show_id"]) != int(show_id):
            raise HTTPException(status_code=409, detail="Another show is already active on this station")
        return

    claimed_show_id = _get_program_workspace_claim_show_id(conn, station_id)
    if claimed_show_id != int(show_id):
        raise HTTPException(
            status_code=409,
            detail="Program workspace must be claimed for this show before going live",
        )


def _ensure_station_show_access(
    auth_user: dict,
    station_id: int,
    conn,
    show_id: int | None = None,
) -> None:
    if auth_user is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if str(auth_user.get("role") or "") == "admin":
        return
    if user_has_permission(auth_user, "shows.manage") or user_has_permission(auth_user, "show.assign.manage"):
        return

    repo = ShowRepository(conn)
    active_session = ShowSessionRepository(conn).get_active_for_station(station_id)
    if active_session is not None:
        active_show_id = int(active_session["show_id"])
        requested_show_id = int(show_id or 0)
        if requested_show_id > 0 and requested_show_id != active_show_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        if repo.is_assigned(active_show_id, int(auth_user["id"])):
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    requested_show_id = int(show_id or 0)
    if requested_show_id > 0:
        requested_show = repo.get(requested_show_id)
        if not requested_show:
            raise HTTPException(status_code=404, detail="Show not found")
        if int(requested_show["station_id"]) != int(station_id):
            raise HTTPException(status_code=400, detail="show_id does not belong to this station")
        if repo.is_assigned(requested_show_id, int(auth_user["id"])):
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    assigned = repo.list_for_user(int(auth_user["id"]), station_id=station_id)
    if not assigned:
        raise HTTPException(status_code=403, detail="Forbidden")
    raise HTTPException(
        status_code=400,
        detail="show_id is required when no active session exists",
    )


@router.get("/")
def list_shows(station_id: int, request: Request):
    user = _request_user(request)
    auth_user = getattr(request.state, "current_user", None) or user
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        if user["role"] == "admin" or user_has_permission(auth_user, "shows.manage") or user_has_permission(auth_user, "show.assign.manage"):
            rows = repo.list_by_station(station_id)
        else:
            rows = repo.list_for_user(user["id"], station_id=station_id)
        return [_show_to_dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/session/current")
def get_current_session(
    station_id: int,
    request: Request,
    show_id: int | None = None,
):
    _request_user(request)
    init_db()
    conn = get_connection()
    try:
        auth_user = getattr(request.state, "current_user", None)
        _ensure_station_show_access(auth_user, station_id, conn, show_id=show_id)
        session = ShowSessionRepository(conn).get_active_for_station(station_id)
        if session:
            show = ShowRepository(conn).get(session["show_id"])
            session["show_name"] = show["name"] if show else ""
        return {"session": session}
    finally:
        conn.close()


@router.get("/{show_id}")
def get_show(show_id: int, request: Request):
    _request_user(request)
    auth_user = getattr(request.state, "current_user", None)
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        row = repo.get(show_id)
        if not row:
            raise HTTPException(status_code=404, detail="Show not found")
        if not (
            user_has_permission(auth_user, "shows.manage")
            or user_has_permission(auth_user, "show.assign.manage")
            or repo.is_assigned(show_id, int(auth_user["id"]))
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        return _show_to_dict(row)
    finally:
        conn.close()


@router.post("/")
def create_show(
    request: Request,
    body: ShowCreatePayload,
    _user=Depends(require_permission("shows.manage")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        show_id = repo.create(
            station_id=body.station_id,
            name=body.name,
            description=body.description,
            color=body.color,
        )
        return _show_to_dict(repo.get(show_id))
    finally:
        conn.close()


@router.put("/{show_id}")
def update_show(
    request: Request,
    show_id: int,
    body: ShowUpdatePayload,
    _user=Depends(require_permission("shows.manage")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        if not repo.get(show_id):
            raise HTTPException(status_code=404, detail="Show not found")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        updated = repo.update(show_id, **fields)
        return _show_to_dict(updated)
    finally:
        conn.close()


@router.delete("/{show_id}")
def delete_show(
    request: Request,
    show_id: int,
    _user=Depends(require_permission("shows.manage")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        if not ShowRepository(conn).delete(show_id):
            raise HTTPException(status_code=404, detail="Show not found")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/{show_id}/assignments")
def list_assignments(
    request: Request,
    show_id: int,
    _user=Depends(require_permission("show.assign.manage")),
):
    _request_user(request)
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        rbac_repo = RbacRepository(conn)
        if not repo.get(show_id):
            raise HTTPException(status_code=404, detail="Show not found")
        return _serialize_assignments(repo, rbac_repo, show_id)
    finally:
        conn.close()


@router.get("/{show_id}/assignment-candidates")
def list_assignment_candidates(
    request: Request,
    show_id: int,
    _user=Depends(require_permission("show.assign.manage")),
):
    _request_user(request)
    init_db()
    conn = get_connection()
    try:
        if not ShowRepository(conn).get(show_id):
            raise HTTPException(status_code=404, detail="Show not found")
        rows = UserRepository(conn).list_users(include_inactive=False)
        return {"items": [_serialize_assignment_candidate(row) for row in rows]}
    finally:
        conn.close()


@router.post("/{show_id}/assign")
def assign_user(
    request: Request,
    show_id: int,
    body: AssignPayload,
    _user=Depends(require_permission("show.assign.manage")),
):
    user = _request_user(request)
    if body.role not in ("dj", "producer"):
        raise HTTPException(status_code=400, detail="role must be 'dj' or 'producer'")
    permission_keys = (
        _normalize_permission_keys(body.permission_keys)
        if body.permission_keys is not None
        else None
    )
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        rbac_repo = RbacRepository(conn)
        if not repo.get(show_id):
            raise HTTPException(status_code=404, detail="Show not found")
        was_in_transaction = conn.in_transaction
        try:
            if not was_in_transaction:
                conn.execute("BEGIN")
            repo.assign(show_id, body.user_id, role=body.role, permission_keys=permission_keys)
            if permission_keys is not None:
                rbac_repo.replace_show_permissions(show_id, body.user_id, permission_keys)
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        return {"ok": True, "assignments": _serialize_assignments(repo, rbac_repo, show_id)}
    finally:
        conn.close()


@router.delete("/{show_id}/assign/{user_id}")
def unassign_user(
    request: Request,
    show_id: int,
    user_id: int,
    _user=Depends(require_permission("show.assign.manage")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        if not ShowRepository(conn).unassign(show_id, user_id):
            raise HTTPException(status_code=404, detail="Assignment not found")
        return {"ok": True}
    finally:
        conn.close()


_AUDIO_TYPES = {"intro", "outro", "break_outro", "break_intro"}
_PATH_COLUMNS = {
    "intro": "intro_path",
    "outro": "outro_path",
    "break_outro": "break_outro_path",
    "break_intro": "break_intro_path",
}


@router.post("/{show_id}/upload-audio")
async def upload_show_audio(
    request: Request,
    show_id: int,
    file: UploadFile = File(...),
    type: str = Form(...),
    _user=Depends(get_current_user),
):
    user = _request_user(request)
    auth_user = getattr(request.state, "current_user", None) or _user
    if not (
        user["role"] == "admin"
        or user_has_permission(auth_user, "shows.manage")
        or user_has_show_permission(auth_user, show_id, "show.jingle_manage")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if type not in _AUDIO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(_AUDIO_TYPES))}",
        )
    init_db()
    conn = get_connection()
    try:
        repo = ShowRepository(conn)
        show = repo.get(show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        target_dir = get_db_path().parent / "media" / "shows" / str(show_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_source_name = sanitize_upload_filename(
            str(file.filename or "audio.mp3"),
            default_stem="audio",
            default_extension=".mp3",
            allowed_extensions=audio_upload_extensions(),
        )
        ext = Path(safe_source_name).suffix or ".mp3"
        safe_name = f"{type}{ext}"
        target_path = target_dir / safe_name
        previous_path = str(show.get(_PATH_COLUMNS[type]) or "").strip()
        await write_upload_to_path(file, target_path)
        column = _PATH_COLUMNS[type]
        repo.update(show_id, **{column: str(target_path.resolve())})
        if previous_path and Path(previous_path).resolve() != target_path.resolve():
            safe_unlink(previous_path, root=get_db_path().parent / "media" / "shows")
        return {
            "ok": True,
            "type": type,
            "file_path": str(target_path.resolve()),
            "original_name": file.filename,
        }
    finally:
        conn.close()


@router.post("/{show_id}/go-live")
def go_live(
    request: Request,
    show_id: int,
    body: GoLivePayload,
    _user=Depends(require_show_permission("show.broadcast")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        show = ShowRepository(conn).get(show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")

        session_repo = ShowSessionRepository(conn)
        existing = session_repo.get_active_for_station(body.station_id)
        if existing:
            raise HTTPException(status_code=409, detail="Active session already exists for this station")
        _require_program_workspace_claim_for_show(conn, body.station_id, show_id)

        # Check the visible program queue rather than the internal continuity
        # queue. Hosts prepare shows in this workspace and hidden/stale
        # continuity rows must not satisfy the go-live safety threshold.
        settings = SettingsRepository(conn).get_station(body.station_id)
        min_tracks = int(settings.get("show_min_queue_tracks", "3") or "3")
        pending_count = len(ProgramQueueRepository(conn).list_items(body.station_id))
        if pending_count < min_tracks:
            raise HTTPException(
                status_code=400,
                detail=f"Host queue needs at least {min_tracks} tracks (has {pending_count})",
            )

        session_id = session_repo.create(
            show_id=show_id, station_id=body.station_id, user_id=user["id"],
        )
        session_repo.update_status(session_id, "going_live")
        ProgramQueueRepository(conn).set_source(body.station_id, "host")
        _log_show_transition(conn, body.station_id, show_id, "preparing", "going_live")

        session = session_repo.get(session_id)
        _broadcast_show_ws("show.going_live", body.station_id, {
            "show_id": show_id,
            "show_name": show["name"],
            "session_id": session_id,
        })
        return {"ok": True, "session": session}
    finally:
        conn.close()


@router.post("/{show_id}/go-break")
def go_break(
    request: Request,
    show_id: int,
    _user=Depends(require_show_permission("show.break_control")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        session_repo = ShowSessionRepository(conn)
        show = ShowRepository(conn).get(show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        session = session_repo.get_active_for_station(int(show["station_id"]))
        if not session or session["show_id"] != show_id:
            raise HTTPException(status_code=400, detail="No active session for this show")
        if session["status"] != "live":
            raise HTTPException(status_code=400, detail="Show must be live to go to break")

        session_repo.update_status(session["id"], "break_outro")
        _log_show_transition(conn, session["station_id"], show_id, "live", "break_outro")
        updated = session_repo.get(session["id"])
        _broadcast_show_ws("show.break_start", session["station_id"], {
            "show_id": show_id,
            "session_id": session["id"],
        })
        return {"ok": True, "session": updated}
    finally:
        conn.close()


@router.post("/{show_id}/end")
def end_show(
    request: Request,
    show_id: int,
    _user=Depends(require_show_permission("show.end")),
):
    user = _request_user(request)
    init_db()
    conn = get_connection()
    try:
        session_repo = ShowSessionRepository(conn)
        show = ShowRepository(conn).get(show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        session = session_repo.get_active_for_station(int(show["station_id"]))
        if not session or session["show_id"] != show_id:
            raise HTTPException(status_code=400, detail="No active session for this show")
        if session["status"] not in ("live", "on_break", "break_outro", "break_intro"):
            raise HTTPException(status_code=400, detail="Show is not in a state that can be ended")

        prev_status = session["status"]
        session_repo.update_status(session["id"], "outro_playing")
        _log_show_transition(conn, session["station_id"], show_id, prev_status, "outro_playing")
        updated = session_repo.get(session["id"])
        _broadcast_show_ws("show.outro_playing", session["station_id"], {
            "show_id": show_id,
            "session_id": session["id"],
        })
        return {"ok": True, "session": updated}
    finally:
        conn.close()
