from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
CAPABILITY_MAX_AGE_SECONDS = 20.0
REQUEST_MAX_AGE_SECONDS = 120.0


def control_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    configured = str(os.getenv("CLEANROOM_DATA_ROOT") or "").strip()
    if configured:
        return Path(configured) / "control"
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return program_data / "RadioTEDU" / "OnAir" / "control"


def capability_path(root: Path | None = None) -> Path:
    return control_root(root) / "backend-reload-capability.json"


def request_path(root: Path | None = None) -> Path:
    return control_root(root) / "backend-reload-request.json"


def new_supervisor_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def publish_supervisor_capability(
    supervisor_token: str,
    *,
    supervisor_pid: int,
    root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    payload = {
        "protocol": PROTOCOL_VERSION,
        "supervisor_token": str(supervisor_token),
        "supervisor_pid": int(supervisor_pid),
        "heartbeat_epoch": float(time.time() if now is None else now),
    }
    _atomic_write_json(capability_path(root), payload)
    return payload


def read_fresh_supervisor_capability(
    *,
    root: Path | None = None,
    now: float | None = None,
    max_age_seconds: float = CAPABILITY_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    payload = _read_json(capability_path(root))
    if not payload or int(payload.get("protocol") or 0) != PROTOCOL_VERSION:
        return None
    token = str(payload.get("supervisor_token") or "")
    heartbeat = float(payload.get("heartbeat_epoch") or 0.0)
    current = float(time.time() if now is None else now)
    if len(token) < 32 or heartbeat <= 0 or current - heartbeat > max_age_seconds:
        return None
    if heartbeat - current > 5.0:
        return None
    return payload


def write_reload_request(
    supervisor_token: str,
    *,
    request_id: str,
    backend_instance_id: str,
    root: Path | None = None,
    now: float | None = None,
    not_before_seconds: float = 3.0,
) -> dict[str, Any]:
    created = float(time.time() if now is None else now)
    payload = {
        "protocol": PROTOCOL_VERSION,
        "supervisor_token": str(supervisor_token),
        "request_id": str(request_id),
        "backend_instance_id": str(backend_instance_id),
        "created_epoch": created,
        "not_before_epoch": created + max(0.0, float(not_before_seconds)),
    }
    _atomic_write_json(request_path(root), payload)
    return payload


def consume_due_reload_request(
    supervisor_token: str,
    *,
    root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    path = request_path(root)
    payload = _read_json(path)
    if not payload:
        return None
    current = float(time.time() if now is None else now)
    valid = (
        int(payload.get("protocol") or 0) == PROTOCOL_VERSION
        and str(payload.get("supervisor_token") or "")
        == str(supervisor_token)
        and bool(str(payload.get("request_id") or ""))
        and current - float(payload.get("created_epoch") or 0.0)
        <= REQUEST_MAX_AGE_SECONDS
    )
    if not valid:
        path.unlink(missing_ok=True)
        return None
    if current < float(payload.get("not_before_epoch") or 0.0):
        return None
    path.unlink(missing_ok=True)
    return payload
