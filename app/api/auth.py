from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.auth.brute_force import BruteForceProtection
from app.auth.dependencies import build_auth_user_payload
from app.auth.jwt_handler import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.auth.password import hash_password, verify_password
from app.config import get_data_root
from app.db import get_connection, init_db
from app.repositories.user_repo import SessionRepository, UserRepository

router = APIRouter()
brute_force = BruteForceProtection()


class LoginPayload(BaseModel):
    username: str
    password: str


class RefreshPayload(BaseModel):
    refresh_token: str


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str


def _refresh_expiry_text() -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return expires_at.strftime("%Y-%m-%d %H:%M:%S")


def _serialize_auth_user(user: dict) -> dict:
    payload = build_auth_user_payload(user)
    return {
        "id": int(payload["id"]),
        "username": str(payload["username"]),
        "display_name": str(payload["display_name"]),
        "role": str(payload["role"]),
        "legacy_role": str(payload["legacy_role"]),
        "effective_permissions": sorted(
            str(permission) for permission in payload.get("effective_permissions") or set()
        ),
        "role_template_ids": sorted(
            int(role_template_id)
            for role_template_id in payload.get("role_template_ids") or set()
        ),
        "last_login_at": payload["last_login_at"],
    }


def _extract_bearer_token(authorization: str | None) -> str:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = raw[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def _load_current_user(authorization: str | None):
    token = _extract_bearer_token(authorization)
    try:
        payload = decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if str(payload.get("type") or "") != "access":
        raise HTTPException(status_code=401, detail="Invalid token")

    init_db()
    conn = get_connection()
    try:
        repo = UserRepository(conn)
        user = repo.get_user_by_id(int(payload["sub"]))
        if user is None or int(user["is_active"]) != 1:
            raise HTTPException(status_code=401, detail="Invalid token")
        return build_auth_user_payload(user, conn=conn)
    finally:
        conn.close()


@router.post("/api/auth/login")
def login(payload: LoginPayload, request: Request):
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    if not brute_force.check_allowed(username):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        sessions = SessionRepository(conn)
        sessions.cleanup_expired()
        user = users.get_user_by_username(username)
        if user is None or int(user["is_active"]) != 1:
            brute_force.record_failure(username)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not verify_password(password, str(user["password_hash"])):
            brute_force.record_failure(username)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        access_token = create_access_token(int(user["id"]), str(user["role"]))
        refresh_token = create_refresh_token(int(user["id"]))
        sessions.create_session(
            user_id=int(user["id"]),
            refresh_token=refresh_token,
            device_info=str(request.headers.get("user-agent", "") or ""),
            ip_address=str(getattr(request.client, "host", "") or ""),
            expires_at=_refresh_expiry_text(),
        )
        users.update_last_login(int(user["id"]))
        brute_force.record_success(username)

        refreshed_user = build_auth_user_payload(users.get_user_by_id(int(user["id"])), conn=conn)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": _serialize_auth_user(refreshed_user),
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    finally:
        conn.close()


@router.post("/api/auth/refresh")
def refresh(payload: RefreshPayload, request: Request):
    try:
        token_payload = decode_token(payload.refresh_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    if str(token_payload.get("type") or "") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    init_db()
    conn = get_connection()
    try:
        users = UserRepository(conn)
        sessions = SessionRepository(conn)
        session = sessions.get_session_by_token(payload.refresh_token)
        if session is None or int(session["revoked"]) != 0:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        user = users.get_user_by_id(int(session["user_id"]))
        if user is None or int(user["is_active"]) != 1:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        resolved_user = build_auth_user_payload(user, conn=conn)

        next_refresh_token = create_refresh_token(int(user["id"]))
        sessions.revoke_session(int(session["id"]))
        sessions.create_session(
            user_id=int(user["id"]),
            refresh_token=next_refresh_token,
            device_info=str(request.headers.get("user-agent", "") or ""),
            ip_address=str(getattr(request.client, "host", "") or ""),
            expires_at=_refresh_expiry_text(),
        )
        return {
            "access_token": create_access_token(
                int(resolved_user["id"]), str(resolved_user["legacy_role"])
            ),
            "refresh_token": next_refresh_token,
            "user": _serialize_auth_user(resolved_user),
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    finally:
        conn.close()


@router.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    user = _load_current_user(authorization)
    init_db()
    conn = get_connection()
    try:
        SessionRepository(conn).revoke_all_user_sessions(int(user["id"]))
        return {"detail": "Logged out"}
    finally:
        conn.close()


@router.get("/api/auth/me")
def me(authorization: str | None = Header(default=None)):
    user = _load_current_user(authorization)
    return _serialize_auth_user(user)


@router.put("/api/auth/password")
def update_password(
    payload: PasswordPayload,
    authorization: str | None = Header(default=None),
):
    user = _load_current_user(authorization)
    if len(str(payload.new_password or "")) < 5:
        raise HTTPException(status_code=400, detail="Password too short")
    if not verify_password(payload.current_password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    init_db()
    conn = get_connection()
    try:
        updated = UserRepository(conn).update_user(
            int(user["id"]),
            password_hash=hash_password(payload.new_password),
        )
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        if str(user["username"] or "").strip().lower() == "admin":
            try:
                (get_data_root() / "initial-admin-password.txt").unlink(missing_ok=True)
            except OSError:
                # Password rotation succeeded; stale credential cleanup must not
                # turn that success into an authentication failure.
                pass
        return {"detail": "Password updated"}
    finally:
        conn.close()
