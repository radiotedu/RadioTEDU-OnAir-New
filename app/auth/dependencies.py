from fastapi import HTTPException, Request

from app.auth.jwt_handler import decode_token
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.user_repo import UserRepository

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
_ALL_ROLES = {"admin", "dj", "producer", "viewer"}
_PUBLIC_API_PATHS = {
    "/api/health/live",
    "/api/health/ready",
    "/api/health-wall",
    "/api/monitor/snapshot",
    "/api/public/stations",
    "/api/public/campaign",
    "/api/public/campaign/vote",
    "/api/watchdog/status",
    "/api/watchdog/repair",
    "/api/watchdog/report",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/ai/model-status",
    "/api/ai/system-info",
    "/api/ai/fast/tts-test",
    "/api/ai/fast/status",
    "/api/ai/fast/warmup",
    "/api/ai/benchmark",
    "/api/guest/redeem",
    "/api/guest/session",
    "/api/guest/mute",
    "/api/guest/consent",
    "/api/ha/internal/heartbeat",
    "/api/ha/internal/vote",
    "/api/ha/internal/audit-anchor",
    "/api/ha/internal/replicate",
    "/api/ha/internal/checkpoint",
}
_ROUTE_ROLE_RULES: list[tuple[str, set[str], set[str]]] = [
    ("/api/auth/", set(_ALL_ROLES), set(_ALL_ROLES)),
    ("/api/users", {"admin"}, {"admin"}),
    ("/api/roles", set(_ALL_ROLES), set(_ALL_ROLES)),
    ("/api/runtime/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/liquidsoap/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/audio/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/queue/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/playout/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/program/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/schedule/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/ads/", {"admin", "producer", "viewer"}, {"admin", "producer"}),
    ("/api/ad-break-sets", {"admin", "producer", "viewer"}, {"admin", "producer"}),
    ("/api/ad-campaigns", {"admin", "producer", "viewer"}, {"admin", "producer"}),
    ("/api/tracks/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/library/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/media/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/scanner/", {"admin", "dj", "producer"}, {"admin", "dj", "producer"}),
    ("/api/playlists/", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/studios", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/guest-recordings", {"admin", "dj", "producer"}, {"admin", "dj", "producer"}),
    ("/api/stations/", {"admin", "dj", "viewer"}, {"admin", "dj"}),
    ("/api/speaker/", {"admin", "dj", "viewer"}, {"admin", "dj"}),
    ("/api/settings/", {"admin"}, {"admin"}),
    ("/api/recovery/", {"admin"}, {"admin"}),
    ("/api/sweeper/", {"admin"}, {"admin"}),
    ("/api/startup-sound/", {"admin"}, {"admin"}),
    ("/api/outbox/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/webrtc/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/soundboard/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/setup/", {"admin", "dj"}, {"admin", "dj"}),
    ("/api/shows/", {"admin", "dj", "producer"}, {"admin", "dj", "producer"}),
    ("/api/logs", set(_ALL_ROLES), {"admin", "dj", "producer"}),
    ("/api/music-usage", {"admin", "dj", "producer"}, {"admin", "dj", "producer"}),
    ("/api/events", set(_ALL_ROLES), {"admin", "dj", "producer"}),
]


def _normalize_api_path(path: str) -> str:
    normalized = str(path or "").strip()
    if normalized.endswith("/") and normalized != "/":
        return normalized.rstrip("/")
    return normalized


def _normalize_legacy_role(role: str) -> str:
    """Map the legacy top-level operator role onto the current admin role."""
    normalized = str(role or "").strip().lower()
    return "admin" if normalized == "superadmin" else normalized


def is_public_api_path(path: str) -> bool:
    normalized = _normalize_api_path(path)
    return normalized in _PUBLIC_API_PATHS


def _path_matches(rule_path: str, actual_path: str) -> bool:
    if actual_path == rule_path:
        return True
    if rule_path.endswith("/"):
        return actual_path.startswith(rule_path)
    return actual_path.startswith(f"{rule_path}/")


def _extract_bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization", "")).strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = header[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def build_auth_user_payload(user: dict, conn=None) -> dict:
    payload = dict(user or {})
    if {
        "legacy_role",
        "effective_permissions",
        "role_template_ids",
        "show_permissions",
    }.issubset(payload.keys()):
        return payload

    payload["legacy_role"] = str(payload.get("role") or "")

    close_conn = False
    if conn is None:
        init_db()
        conn = get_connection()
        close_conn = True

    try:
        repo = RbacRepository(conn)
        user_id = int(payload["id"])
        payload["role_template_ids"] = repo.list_user_role_ids(user_id)
        payload["effective_permissions"] = repo.list_effective_global_permissions(user_id)
        payload["show_permissions"] = repo.list_user_show_permissions(user_id)
        return payload
    finally:
        if close_conn:
            conn.close()


def user_has_permission(user: dict, permission_key: str) -> bool:
    return permission_key in set(user.get("effective_permissions") or set())


def user_has_show_permission(user: dict, show_id: int, permission_key: str) -> bool:
    show_permissions = dict(user.get("show_permissions") or {})
    direct = show_permissions.get(int(show_id))
    if direct is None:
        direct = show_permissions.get(str(int(show_id)))
    return permission_key in _normalize_permission_values(direct)


def _normalize_permission_values(permission_values) -> set[str]:
    if permission_values is None:
        return set()
    if isinstance(permission_values, set):
        return {
            str(permission).strip()
            for permission in permission_values
            if str(permission).strip()
        }
    if isinstance(permission_values, (list, tuple)):
        return {
            str(permission).strip()
            for permission in permission_values
            if str(permission).strip()
        }
    return {
        str(permission).strip()
        for permission in set(permission_values)
        if str(permission).strip()
    }


def user_has_any_show_permission(user: dict, *permission_keys: str) -> bool:
    required_permissions = {
        str(permission_key).strip()
        for permission_key in permission_keys
        if str(permission_key).strip()
    }
    if not required_permissions:
        return False

    show_permissions = user.get("show_permissions") or {}
    values = show_permissions.values() if isinstance(show_permissions, dict) else []
    for permission_values in values:
        if _normalize_permission_values(permission_values) & required_permissions:
            return True
    return False


def require_permission(*permission_keys: str):
    required_permissions = tuple(
        str(permission_key).strip()
        for permission_key in permission_keys
        if str(permission_key).strip()
    )
    if not required_permissions:
        raise ValueError("require_permission requires at least one permission key")

    async def dependency(request: Request):
        user = await get_current_user(request)
        if not all(
            user_has_permission(user, permission_key)
            for permission_key in required_permissions
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dependency


def require_any_permission(*permission_keys: str):
    required_permissions = tuple(
        str(permission_key).strip()
        for permission_key in permission_keys
        if str(permission_key).strip()
    )
    if not required_permissions:
        raise ValueError("require_any_permission requires at least one permission key")

    async def dependency(request: Request):
        user = await get_current_user(request)
        if not any(
            user_has_permission(user, permission_key)
            for permission_key in required_permissions
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dependency


def require_show_permission(permission_key: str):
    required_permission = str(permission_key).strip()

    async def dependency(request: Request, show_id: int):
        user = await get_current_user(request)
        if str(user.get("role") or "") == "admin":
            return user
        if not user_has_show_permission(user, show_id, required_permission):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dependency


def _load_user_from_access_token(token: str):
    try:
        payload = decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if str(payload.get("type") or "") != "access":
        raise HTTPException(status_code=401, detail="Invalid token")

    init_db()
    conn = get_connection()
    try:
        user = UserRepository(conn).get_user_by_id(int(payload["sub"]))
        if user is None or int(user["is_active"]) != 1:
            raise HTTPException(status_code=401, detail="Invalid token")
        return build_auth_user_payload(user, conn=conn)
    finally:
        conn.close()


async def get_optional_user(request: Request):
    existing = getattr(request.state, "current_user", None)
    if existing is not None:
        return existing
    header = str(request.headers.get("authorization", "")).strip()
    if not header:
        return None
    user = _load_user_from_access_token(_extract_bearer_token(request))
    request.state.current_user = user
    return user


async def get_current_user(request: Request):
    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return user


def role_is_allowed_for_request(role: str, path: str, method: str) -> bool:
    normalized_path = str(path or "")
    normalized_method = str(method or "GET").upper()
    allowed: set[str] | None = None
    for rule_path, read_roles, write_roles in _ROUTE_ROLE_RULES:
        if _path_matches(rule_path, normalized_path):
            allowed = read_roles if normalized_method in READ_ONLY_METHODS else write_roles
            break
    if allowed is None:
        allowed = set(_ALL_ROLES) if normalized_method in READ_ONLY_METHODS else {"admin"}
    return _normalize_legacy_role(role) in allowed


def _normalized_effective_permissions(user: dict) -> set[str]:
    return _normalize_permission_values(user.get("effective_permissions") or set())


def user_is_allowed_for_request(user: dict, path: str, method: str) -> bool:
    normalized_path = str(path or "")
    normalized_method = str(method or "GET").upper()
    permissions = _normalized_effective_permissions(user)
    user_role = str(user.get("role") or "")

    if _path_matches("/api/stream-config/", normalized_path):
        if _normalize_legacy_role(user_role) == "admin":
            return True
        if normalized_method in READ_ONLY_METHODS:
            return bool(permissions & {"stream.configure_basic", "stream.configure_advanced"})
        if normalized_path.endswith("/rollback"):
            return "stream.configure_advanced" in permissions
        return "stream.configure_basic" in permissions

    if _path_matches("/api/liquidsoap/", normalized_path):
        if _normalize_legacy_role(user_role) == "admin":
            return True
        if normalized_path.endswith("/status"):
            return (
                bool(permissions & {"stations.view", "stations.edit"})
                or user_has_any_show_permission(
                    user,
                    "show.broadcast",
                    "show.queue_edit",
                    "show.jingle_manage",
                    "show.break_control",
                    "show.end",
                )
            )
        if normalized_path.endswith("/program/music") or normalized_path.endswith("/duck"):
            return user_has_any_show_permission(user, "show.broadcast")
        if normalized_path.endswith("/push") or normalized_path.endswith("/skip") or normalized_path.endswith("/cart"):
            return user_has_any_show_permission(user, "show.broadcast")

    if role_is_allowed_for_request(user_role, path, method):
        return True

    if _path_matches("/api/users", normalized_path):
        if normalized_method in READ_ONLY_METHODS:
            return bool(permissions & {"users.manage", "users.reset_password"})
        if normalized_path.endswith("/reset-password"):
            return bool(permissions & {"users.manage", "users.reset_password"})
        return "users.manage" in permissions

    if normalized_path == "/api/stations" or _path_matches("/api/stations/", normalized_path):
        if normalized_path.endswith("/output"):
            if normalized_method in READ_ONLY_METHODS:
                return bool(permissions & {"stations.view", "stations.edit"})
            return "stations.edit" in permissions
        if normalized_method in READ_ONLY_METHODS:
            return bool(
                permissions
                & {"stations.view", "stations.create", "stations.edit", "stations.delete"}
            )
        if normalized_method == "DELETE":
            return "stations.delete" in permissions
        if normalized_path.endswith("/active"):
            return "stations.edit" in permissions
        return "stations.create" in permissions

    if _path_matches("/api/speaker/", normalized_path):
        if normalized_method in READ_ONLY_METHODS:
            return bool(permissions & {"stations.view", "stations.edit"})
        return "stations.edit" in permissions

    if _path_matches("/api/library/import/ytdlp", normalized_path):
        return "downloads.use" in permissions

    if _path_matches("/api/logs", normalized_path) or _path_matches("/api/events", normalized_path):
        return "logs.view" in permissions

    if _path_matches("/api/soundboard/", normalized_path):
        if normalized_method in READ_ONLY_METHODS:
            return bool(permissions & {"soundboard.view", "soundboard.play", "soundboard.manage"})
        if normalized_path.endswith("/play") or normalized_path.endswith("/stop"):
            return bool(permissions & {"soundboard.play", "soundboard.manage"})
        return "soundboard.manage" in permissions

    if normalized_path == "/api/program" or _path_matches("/api/program/", normalized_path):
        if normalized_method in READ_ONLY_METHODS:
            return "program.panel.open" in permissions or user_has_any_show_permission(
                user,
                "show.broadcast",
                "show.queue_edit",
                "show.jingle_manage",
                "show.break_control",
                "show.end",
            )
        if normalized_path.endswith("/queue/source"):
            return user_has_any_show_permission(user, "show.broadcast")
        if "/queue" in normalized_path:
            return user_has_any_show_permission(user, "show.queue_edit")
        return "program.panel.open" in permissions

    if normalized_path == "/api/shows" or _path_matches("/api/shows/", normalized_path):
        if normalized_method in READ_ONLY_METHODS:
            return (
                bool(permissions & {"shows.view", "shows.manage", "show.assign.manage", "program.panel.open"})
                or user_has_any_show_permission(
                    user,
                    "show.broadcast",
                    "show.queue_edit",
                    "show.jingle_manage",
                    "show.break_control",
                    "show.end",
                )
            )
        if normalized_path.endswith("/assign") or "/assign/" in normalized_path:
            return "show.assign.manage" in permissions
        if normalized_path.endswith("/upload-audio"):
            return "shows.manage" in permissions or user_has_any_show_permission(user, "show.jingle_manage")
        if normalized_path.endswith("/go-live"):
            return user_has_any_show_permission(user, "show.broadcast")
        if normalized_path.endswith("/go-break"):
            return user_has_any_show_permission(user, "show.break_control")
        if normalized_path.endswith("/end"):
            return user_has_any_show_permission(user, "show.end")
        return "shows.manage" in permissions

    return False


def require_role(*roles: str):
    allowed_roles = {
        _normalize_legacy_role(role)
        for role in roles
        if str(role).strip()
    }

    async def dependency(request: Request):
        user = await get_current_user(request)
        if _normalize_legacy_role(user["role"]) not in allowed_roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return dependency


def user_is_superadmin(user: dict) -> bool:
    return _normalize_legacy_role((user or {}).get("role") or "") == "admin"


def user_has_unrestricted_station_access(user: dict) -> bool:
    if user_is_superadmin(user):
        return True
    permissions = _normalized_effective_permissions(user or {})
    return bool(permissions & {"stations.view", "stations.edit", "stations.create", "stations.delete"})


def _assigned_station_ids(user: dict) -> set[int]:
    raw_values = []
    for key in ("station_ids", "assigned_station_ids", "stations"):
        value = (user or {}).get(key)
        if isinstance(value, (list, tuple, set)):
            raw_values.extend(value)
        elif value not in (None, ""):
            raw_values.append(value)
    for key in ("station_id", "assigned_station_id"):
        value = (user or {}).get(key)
        if value not in (None, ""):
            raw_values.append(value)

    station_ids: set[int] = set()
    for value in raw_values:
        try:
            station_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return station_ids


def filter_station_rows_for_user(user: dict, rows: list) -> list:
    if user_has_unrestricted_station_access(user):
        return list(rows or [])
    allowed = _assigned_station_ids(user or {})
    if not allowed:
        return []

    filtered = []
    for row in rows or []:
        try:
            station_id = int(row["id"])
        except Exception:
            try:
                station_id = int(row.get("id"))
            except Exception:
                continue
        if station_id in allowed:
            filtered.append(row)
    return filtered
