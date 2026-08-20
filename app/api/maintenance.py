from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.dependencies import require_role
from app.db import get_connection
from app.engine.playout_state import reconcile_all_startup
from app.runtime_identity import BACKEND_INSTANCE_ID, BACKEND_PROCESS_ID
from app.services.backend_reload_control import (
    read_fresh_supervisor_capability,
    write_reload_request,
)


router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback_request(request: Request) -> bool:
    return bool(request.client and str(request.client.host).lower() in _LOOPBACK_HOSTS)


def _prepare_for_supervised_reload() -> dict:
    from app.api.runtime import runtime_registry, worker_loop_manager

    loop_result = worker_loop_manager.stop_all()
    runtime_result = runtime_registry.stop_all()
    conn = get_connection()
    try:
        reconciliation = reconcile_all_startup(conn)
    finally:
        conn.close()
    return {
        "worker_loops": loop_result,
        "runtimes": runtime_result,
        "reconciliation": reconciliation,
    }


@router.post("/backend/reload", status_code=status.HTTP_202_ACCEPTED)
def request_supervised_backend_reload(
    request: Request,
    _user=Depends(require_role("admin")),
):
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="loopback_request_required")
    capability = read_fresh_supervisor_capability()
    if capability is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The updated Windows supervisor is not active yet. Restart "
                "RadioTEDU.BroadcastSupervisor as Administrator or reboot once."
            ),
        )
    try:
        preservation = _prepare_for_supervised_reload()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="backend_reload_preparation_failed",
        ) from exc
    request_id = uuid.uuid4().hex
    write_reload_request(
        str(capability["supervisor_token"]),
        request_id=request_id,
        backend_instance_id=BACKEND_INSTANCE_ID,
    )
    return {
        "accepted": True,
        "request_id": request_id,
        "previous_backend_instance_id": BACKEND_INSTANCE_ID,
        "previous_backend_process_id": BACKEND_PROCESS_ID,
        "playlist_preserved": True,
        "restart_delay_seconds": 3,
        "preservation": preservation,
    }
