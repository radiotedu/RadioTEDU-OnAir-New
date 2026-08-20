import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user, require_permission, user_has_permission
from app.auth.permissions import GLOBAL_PERMISSION_GROUPS, GLOBAL_PERMISSION_KEYS
from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository

router = APIRouter()


class RoleTemplatePayload(BaseModel):
    name: str
    description: str = ""
    permission_keys: list[str] = Field(default_factory=list)


class RoleTemplateUpdatePayload(BaseModel):
    name: str | None = None
    description: str | None = None
    permission_keys: list[str] | None = None


def _require_roles_read(user=Depends(get_current_user)):
    if not (
        user_has_permission(user, "roles.manage")
        or user_has_permission(user, "users.manage")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _serialize_role_template(row: dict, permission_keys: set[str]) -> dict:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "is_system": bool(row["is_system"]),
        "is_active": bool(row["is_active"]),
        "permission_keys": sorted(str(permission) for permission in permission_keys),
    }


def _normalize_permission_keys(permission_keys: list[str]) -> set[str]:
    normalized = {
        str(permission_key).strip()
        for permission_key in (permission_keys or [])
        if str(permission_key).strip()
    }
    invalid = sorted(
        permission
        for permission in normalized
        if permission not in GLOBAL_PERMISSION_KEYS
    )
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid permission key: {invalid[0]}",
        )
    return normalized


def _load_role_template(repo: RbacRepository, role_template_id: int) -> dict:
    row = repo.get_role_template(role_template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Role template not found")
    return row


def _serialize_role_template_by_id(repo: RbacRepository, role_template_id: int) -> dict:
    row = _load_role_template(repo, role_template_id)
    permission_keys = repo.list_role_permissions(role_template_id)
    return _serialize_role_template(row, permission_keys)


def _serialize_permission_groups() -> dict[str, list[str]]:
    return {
        group: sorted(str(permission) for permission in permissions)
        for group, permissions in GLOBAL_PERMISSION_GROUPS.items()
    }


def _raise_conflict_on_integrity_error(exc: sqlite3.IntegrityError) -> None:
    raise HTTPException(status_code=409, detail="Role template name already exists") from exc


@router.get("/api/roles")
def list_roles(_user=Depends(_require_roles_read)):
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        rows = repo.list_role_templates(include_inactive=True)
        items = [
            _serialize_role_template(row, repo.list_role_permissions(int(row["id"])))
            for row in rows
        ]
        return {"items": items, "permission_groups": _serialize_permission_groups()}
    finally:
        conn.close()


@router.post("/api/roles", status_code=201)
def create_role(payload: RoleTemplatePayload, _user=Depends(require_permission("roles.manage"))):
    permission_keys = _normalize_permission_keys(payload.permission_keys)
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        conn.execute("BEGIN")
        role_id = repo.create_role_template(
            payload.name,
            payload.description,
            False,
        )
        repo.replace_role_permissions(role_id, permission_keys)
        conn.commit()
        return _serialize_role_template_by_id(repo, role_id)
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        _raise_conflict_on_integrity_error(exc)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@router.put("/api/roles/{role_id}")
def update_role(
    role_id: int,
    payload: RoleTemplateUpdatePayload,
    _user=Depends(require_permission("roles.manage")),
):
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        row = _load_role_template(repo, role_id)
        if int(row["is_system"]) == 1:
            raise HTTPException(
                status_code=400,
                detail="System role templates cannot be updated",
            )
        permission_keys = (
            _normalize_permission_keys(payload.permission_keys)
            if payload.permission_keys is not None
            else None
        )
        update_fields = {}
        if payload.name is not None:
            update_fields["name"] = payload.name
        if payload.description is not None:
            update_fields["description"] = payload.description
        if update_fields:
            conn.execute("BEGIN")
            updated = repo.update_role_template(role_id, **update_fields)
            if not updated:
                raise HTTPException(status_code=404, detail="Role template not found")
        if permission_keys is not None:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            repo.replace_role_permissions(role_id, permission_keys)
        if conn.in_transaction:
            conn.commit()
        return _serialize_role_template_by_id(repo, role_id)
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.rollback()
        _raise_conflict_on_integrity_error(exc)
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


@router.delete("/api/roles/{role_id}")
def deactivate_role(role_id: int, _user=Depends(require_permission("roles.manage"))):
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        row = _load_role_template(repo, role_id)
        if int(row["is_system"]) == 1:
            raise HTTPException(
                status_code=400,
                detail="System role templates cannot be deactivated",
            )
        updated = repo.update_role_template(role_id, is_active=False)
        if not updated:
            raise HTTPException(status_code=404, detail="Role template not found")
        return {"ok": True}
    finally:
        conn.close()
