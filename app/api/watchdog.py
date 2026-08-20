from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user, user_has_permission
from app.security.watchdog_token import watchdog_request_is_valid
from app.services.audio_watchdog import audio_watchdog_service


router = APIRouter(tags=["audio-watchdog"])


class WatchdogRepairPayload(BaseModel):
    station_ids: list[int] = Field(default_factory=list, max_length=6)
    repair_managed_profiles: bool = False


class WatchdogReportPayload(BaseModel):
    status: str = Field(min_length=1, max_length=40)
    message: str = Field(default="", max_length=500)
    failed_station_ids: list[int] = Field(default_factory=list, max_length=6)
    managed_profiles_ok: bool = False


async def _require_watchdog_or_operator(request: Request):
    if watchdog_request_is_valid(request):
        return {"role": "watchdog"}
    try:
        user = await get_current_user(request)
    except HTTPException as exc:
        raise HTTPException(status_code=401, detail="Watchdog authentication required") from exc
    if not user_has_permission(user, "stations.edit"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


@router.get("/api/watchdog/status")
async def watchdog_status(request: Request):
    await _require_watchdog_or_operator(request)
    return audio_watchdog_service.snapshot()


@router.post("/api/watchdog/repair")
async def watchdog_repair(payload: WatchdogRepairPayload, request: Request):
    await _require_watchdog_or_operator(request)
    try:
        result = audio_watchdog_service.repair(
            station_ids=payload.station_ids,
            repair_managed_profiles=bool(payload.repair_managed_profiles),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not bool(result.get("ok")):
        raise HTTPException(status_code=503, detail="watchdog_repair_incomplete")
    return result


@router.post("/api/watchdog/report")
def watchdog_report(payload: WatchdogReportPayload, request: Request):
    if not watchdog_request_is_valid(request):
        raise HTTPException(status_code=401, detail="Watchdog authentication required")
    return audio_watchdog_service.record_report(payload.model_dump())
