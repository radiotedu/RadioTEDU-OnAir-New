from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.services.recovery_points import recovery_point_service
from app.services.diagnostic_bundle import (
    create_radio_diagnostic_bundle,
    list_radio_diagnostic_bundles,
    resolve_radio_diagnostic_bundle,
)

router = APIRouter()


class RecoveryPointCreate(BaseModel):
    tier: str = "daily"


def _public_point(row) -> dict:
    path = Path(str(row["file_path"] or ""))
    return {
        "created_at": str(row["created_at"] or ""),
        "file_name": path.name,
        "id": int(row["id"]),
        "integrity_status": str(row["integrity_status"] or "unknown"),
        "size_bytes": int(row["size_bytes"] or 0),
        "tier": str(row["tier"] or ""),
        "verified_at": str(row["verified_at"] or ""),
    }


def _diagnostic_health_snapshot() -> dict:
    from app.api.health import health

    payload = dict(health() or {})
    return {
        "database": {
            key: payload.get("database", {}).get(key)
            for key in ("healthy", "integrity", "state")
        },
        "engine_running": bool(payload.get("engine_running")),
        "overall_state": str(payload.get("overall_state") or "unknown"),
        "runtime": payload.get("runtime") or {},
        "runtime_branch_health": payload.get("runtime_branch_health") or {},
        "station_id": int(payload.get("station_id") or 0),
        "status": str(payload.get("status") or "unknown"),
        "worker_loop": payload.get("worker_loop") or {},
    }


@router.get("/api/recovery/diagnostics")
def list_diagnostic_bundles():
    bundles = list_radio_diagnostic_bundles()
    return {
        "bundles": [
            {
                **item,
                "download_url": f"/api/recovery/diagnostics/{item['name']}",
            }
            for item in bundles
        ]
    }


@router.post("/api/recovery/diagnostics")
def create_diagnostic_bundle():
    try:
        result = create_radio_diagnostic_bundle(
            health=_diagnostic_health_snapshot()
        )
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Diagnostic bundle could not be created safely",
        ) from exc
    return {
        **result,
        "download_url": f"/api/recovery/diagnostics/{result['name']}",
    }


@router.get("/api/recovery/diagnostics/{name}")
def download_diagnostic_bundle(name: str):
    try:
        path = resolve_radio_diagnostic_bundle(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/zip",
    )


@router.get("/api/recovery/points")
def list_recovery_points(limit: int = 25):
    init_db()
    safe_limit = max(1, min(100, int(limit)))
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, tier, file_path, size_bytes, integrity_status, "
            "created_at, verified_at FROM recovery_points "
            "ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return {"points": [_public_point(row) for row in rows]}
    finally:
        conn.close()


@router.post("/api/recovery/points")
def create_recovery_point(payload: RecoveryPointCreate):
    try:
        result = recovery_point_service.create(payload.tier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Recovery point could not be created safely"
        ) from exc
    return {
        "file_name": Path(str(result["file_path"])).name,
        "tier": str(result["tier"]),
        "verified": bool(result["verified"]),
    }


@router.post("/api/recovery/points/{point_id}/verify")
def verify_recovery_point(point_id: int):
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, file_path, sha256 FROM recovery_points WHERE id=?",
            (int(point_id),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Recovery point not found")
        try:
            result = recovery_point_service.verify_restore(
                str(row["file_path"]), expected_sha256=str(row["sha256"])
            )
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=409, detail="Recovery point could not be verified safely"
            ) from exc
        valid = bool(result.get("valid"))
        conn.execute(
            "UPDATE recovery_points SET integrity_status=?, verified_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            ("ok" if valid else "failed", int(point_id)),
        )
        conn.commit()
        return {"id": int(point_id), "valid": valid}
    finally:
        conn.close()


@router.post("/api/recovery/points/{point_id}/stage-restore")
def stage_recovery_point_restore(point_id: int):
    """Prepare an offline supervisor restore without replacing the live DB."""
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, file_path, sha256, integrity_status FROM recovery_points "
            "WHERE id=?",
            (int(point_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Recovery point not found")
    if str(row["integrity_status"] or "") != "ok":
        raise HTTPException(
            status_code=409,
            detail="Recovery point must pass verification before staging",
        )
    try:
        result = recovery_point_service.stage_restore(
            str(row["file_path"]), expected_sha256=str(row["sha256"])
        )
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Recovery point could not be staged safely",
        ) from exc
    return {
        "id": int(point_id),
        "plan_id": str(result["plan_id"]),
        "restart_required": bool(result["restart_required"]),
        "staged": bool(result["staged"]),
    }
