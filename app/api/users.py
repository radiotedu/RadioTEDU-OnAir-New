import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import build_auth_user_payload
from app.auth.dependencies import require_any_permission, require_permission
from app.auth.password import hash_password
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.user_repo import UserRepository

router = APIRouter()

_LEGACY_ROLE_TEMPLATE_NAMES = {
    "admin": "Legacy Admin",
    "dj": "Legacy DJ",
    "producer": "Legacy Producer",
    "viewer": "Legacy Viewer",
}


class UserCreatePayload(BaseModel):
    username: str
    display_name: str
    password: str
    role: str
    role_template_ids: list[int] = Field(default_factory=list)


class UserUpdatePayload(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    role_template_ids: list[int] | None = None


class ResetPasswordPayload(BaseModel):
    new_password: str


def _legacy_role_template_ids(repo: RbacRepository, role: str) -> set[int]:
    template_name = _LEGACY_ROLE_TEMPLATE_NAMES.get(str(role))
    if template_name is None:
        return set()
    for row in repo.list_role_templates(include_inactive=True):
        if str(row["name"]) == template_name:
            return {int(row["id"])}
    return set()


def _validate_role_template_ids(repo: RbacRepository, role_template_ids: set[int]) -> None:
    for role_template_id in sorted(role_template_ids):
        if repo.get_role_template(role_template_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role template id: {role_template_id}",
            )


def _resolve_role_template_ids(
    repo: RbacRepository,
    *,
    explicit_role_template_ids: list[int] | None,
    role: str | None,
    current_user_id: int | None = None,
) -> set[int]:
    if explicit_role_template_ids is not None:
        resolved = {int(role_template_id) for role_template_id in explicit_role_template_ids}
    elif role is not None:
        resolved = _legacy_role_template_ids(repo, role)
    elif current_user_id is not None:
        resolved = repo.list_user_role_ids(current_user_id)
    else:
        resolved = set()
    _validate_role_template_ids(repo, resolved)
    return resolved


def _field_was_set(payload: BaseModel, field_name: str) -> bool:
    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    return str(field_name) in set(fields_set or set())


def _serialize_user(row, conn) -> dict:
    payload = build_auth_user_payload(row, conn=conn)
    return {
        "id": int(payload["id"]),
        "username": str(payload["username"]),
        "display_name": str(payload["display_name"]),
        "role": str(payload["role"]),
        "legacy_role": str(payload["legacy_role"]),
        "effective_permissions": sorted(
            str(permission)
            for permission in payload.get("effective_permissions") or set()
        ),
        "role_template_ids": sorted(
            int(role_template_id)
            for role_template_id in payload.get("role_template_ids") or set()
        ),
        "is_active": bool(payload["is_active"]),
        "last_login_at": payload["last_login_at"],
        "avatar_url": payload["avatar_url"],
    }


@router.get("/api/users")
def list_users(_user=Depends(require_any_permission("users.manage", "users.reset_password"))):
    init_db()
    conn = get_connection()
    try:
        rows = UserRepository(conn).list_users(include_inactive=True)
        return {"items": [_serialize_user(row, conn) for row in rows]}
    finally:
        conn.close()


@router.post("/api/users", status_code=201)
def create_user(payload: UserCreatePayload, _user=Depends(require_permission("users.manage"))):
    if len(str(payload.password or "")) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
    init_db()
    conn = get_connection()
    try:
        repo = UserRepository(conn)
        rbac = RbacRepository(conn)
        if repo.get_user_by_username(payload.username) is not None:
            raise HTTPException(status_code=409, detail="Username already exists")
        explicit_role_template_ids = (
            payload.role_template_ids
            if _field_was_set(payload, "role_template_ids")
            else None
        )
        role_template_ids = _resolve_role_template_ids(
            rbac,
            explicit_role_template_ids=explicit_role_template_ids,
            role=payload.role,
        )
        conn.execute("BEGIN")
        user_id = repo.create_user(
            username=payload.username,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
            role=payload.role,
            commit=False,
        )
        rbac.replace_user_roles(user_id, role_template_ids)
        conn.commit()
        row = repo.get_user_by_id(user_id)
        return _serialize_user(row, conn)
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    except HTTPException:
        if conn.in_transaction:
            conn.rollback()
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/api/users/{user_id}")
def get_user(user_id: int, _user=Depends(require_any_permission("users.manage", "users.reset_password"))):
    init_db()
    conn = get_connection()
    try:
        row = UserRepository(conn).get_user_by_id(user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        return _serialize_user(row, conn)
    finally:
        conn.close()


@router.put("/api/users/{user_id}")
def update_user(user_id: int, payload: UserUpdatePayload, _user=Depends(require_permission("users.manage"))):
    init_db()
    conn = get_connection()
    try:
        repo = UserRepository(conn)
        rbac = RbacRepository(conn)
        current_row = repo.get_user_by_id(user_id)
        if current_row is None:
            raise HTTPException(status_code=404, detail="User not found")
        if payload.role_template_ids is not None:
            role_template_ids = _resolve_role_template_ids(
                rbac,
                explicit_role_template_ids=payload.role_template_ids,
                role=None,
            )
        elif payload.role is not None:
            role_template_ids = _resolve_role_template_ids(
                rbac,
                explicit_role_template_ids=None,
                role=payload.role,
            )
        else:
            role_template_ids = _resolve_role_template_ids(
                rbac,
                explicit_role_template_ids=None,
                role=None,
                current_user_id=user_id,
            )

        update_fields = {
            "display_name": payload.display_name,
            "role": payload.role,
            "is_active": (
                None if payload.is_active is None else int(bool(payload.is_active))
            ),
        }
        update_fields = {
            key: value for key, value in update_fields.items() if value is not None
        }

        conn.execute("BEGIN")
        if update_fields:
            updated = repo.update_user(user_id, commit=False, **update_fields)
            if not updated:
                raise HTTPException(status_code=404, detail="User not found")
        rbac.replace_user_roles(user_id, role_template_ids)
        conn.commit()
        row = repo.get_user_by_id(user_id)
        return _serialize_user(row, conn)
    except HTTPException:
        if conn.in_transaction:
            conn.rollback()
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@router.delete("/api/users/{user_id}")
def deactivate_user(user_id: int, _user=Depends(require_permission("users.manage"))):
    init_db()
    conn = get_connection()
    try:
        ok = UserRepository(conn).deactivate_user(user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="User not found")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordPayload,
    _user=Depends(require_any_permission("users.manage", "users.reset_password")),
):
    if len(str(payload.new_password or "")) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
    init_db()
    conn = get_connection()
    try:
        repo = UserRepository(conn)
        ok = repo.update_user(user_id, password_hash=hash_password(payload.new_password))
        if not ok:
            raise HTTPException(status_code=404, detail="User not found")
        return {"detail": "Password reset"}
    finally:
        conn.close()
