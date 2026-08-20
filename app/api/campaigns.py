from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import require_permission
from app.db import get_connection, init_db
from app.middleware.rate_limit import SlidingWindowRateLimiter
from app.services.broadcast_campaign import BroadcastCampaignService


router = APIRouter()
_vote_rate_limiter = SlidingWindowRateLimiter()


class CampaignPayload(BaseModel):
    name: str = Field(default="RadioTEDU No-Copyright Month", min_length=1, max_length=160)
    starts_at: str = Field(min_length=10, max_length=80)
    ends_at: str = Field(min_length=10, max_length=80)
    enabled: bool = True
    voting_enabled: bool = True
    ai_enabled: bool = True


class NormalizePayload(BaseModel):
    dry_run: bool = True


class CreateGenreRoundPayload(BaseModel):
    duration_seconds: int = Field(default=45, ge=15, le=600)


class ResolveGenreRoundPayload(BaseModel):
    force: bool = False


class PublicGenreVotePayload(BaseModel):
    genre: str = Field(min_length=1, max_length=40)
    voter_id: str = Field(default="", max_length=160)


def _campaign_service():
    init_db()
    conn = get_connection()
    return conn, BroadcastCampaignService(conn)


def _safe_public_status(status: dict) -> dict:
    if not status.get("configured"):
        return status
    return {
        "configured": True,
        "active": bool(status.get("active")),
        "state": str(status.get("state") or "disabled"),
        "name": str(status.get("name") or ""),
        "starts_at": str(status.get("starts_at") or ""),
        "ends_at": str(status.get("ends_at") or ""),
        "voting_enabled": bool(status.get("voting_enabled")),
        "genres": [
            {
                "genre": str(item.get("genre") or ""),
                "station_name": str(item.get("station_name") or ""),
                "eligible_tracks": int(item.get("eligible_tracks") or 0),
            }
            for item in status.get("stations") or []
        ],
        "round": status.get("round"),
    }


@router.get("/api/campaign")
def get_campaign(_user=Depends(require_permission("stations.view"))):
    conn, service = _campaign_service()
    try:
        return service.status()
    finally:
        conn.close()


@router.put("/api/campaign")
def save_campaign(
    payload: CampaignPayload,
    _user=Depends(require_permission("stations.edit")),
):
    conn, service = _campaign_service()
    try:
        try:
            return service.save_campaign(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/api/campaign/normalize-track-names")
def normalize_campaign_track_names(
    payload: NormalizePayload,
    _user=Depends(require_permission("library.edit")),
):
    conn, service = _campaign_service()
    try:
        try:
            return service.normalize_eligible_track_names(dry_run=bool(payload.dry_run))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/api/campaign/voting/round")
def create_genre_voting_round(
    payload: CreateGenreRoundPayload,
    _user=Depends(require_permission("stations.edit")),
):
    conn, service = _campaign_service()
    try:
        try:
            return service.create_round(duration_seconds=payload.duration_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/api/campaign/voting/resolve")
def resolve_genre_voting_round(
    payload: ResolveGenreRoundPayload,
    _user=Depends(require_permission("stations.edit")),
):
    conn, service = _campaign_service()
    try:
        try:
            return service.resolve_round(force=bool(payload.force))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/api/public/campaign")
def public_campaign_status():
    conn, service = _campaign_service()
    try:
        return _safe_public_status(service.status())
    finally:
        conn.close()


@router.post("/api/public/campaign/vote")
def public_campaign_vote(payload: PublicGenreVotePayload, request: Request):
    client_host = str(request.client.host if request.client else "unknown")[:120]
    if not _vote_rate_limiter.allow(client_host, limit=20, window_sec=60):
        raise HTTPException(status_code=429, detail="vote rate limit exceeded")
    voter_material = "|".join(
        (
            client_host,
            str(request.headers.get("user-agent") or "")[:300],
            str(payload.voter_id or "anonymous")[:160],
        )
    )
    voter_hash = hashlib.sha256(voter_material.encode("utf-8", errors="replace")).hexdigest()
    conn, service = _campaign_service()
    try:
        try:
            current = service.record_vote(genre=payload.genre, voter_hash=voter_hash)
            return {"accepted": True, "round": current}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        conn.close()
