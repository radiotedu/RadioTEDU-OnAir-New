"""
Fast AI Host API routes.

Provides endpoints that use the pre-loaded AI host service for instant
response times without cold-start delays.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import get_connection
from app.repositories.settings_repo import SettingsRepository
from app.services.ai_host_fast import get_ai_host_fast

_log = logging.getLogger("cleanroom.api.ai_host_fast")

router = APIRouter(prefix="/api/ai/fast", tags=["AI Host Fast"])


# ── Request/Response Models ──────────────────────────────────────────────────

class TrackIntroRequest(BaseModel):
    station_id: int = 1
    station_name: str = "Main Radio"
    title: str
    artist: str = ""
    settings: dict[str, Any] | None = None


class StationIdRequest(BaseModel):
    station_id: int = 1
    station_name: str = "Main Radio"
    settings: dict[str, Any] | None = None


class TTSTestRequest(BaseModel):
    text: str = "Hello! This is a voice quality test. How do I sound?"
    preset: str = "warm_radio_host"
    station_id: int = 1
    station_name: str = "Main Radio"


class TTSTestResponse(BaseModel):
    success: bool
    audio_path: str | None = None
    duration_seconds: float | None = None
    text_used: str = ""
    preset_used: str = ""
    error: str | None = None


class FastStatusResponse(BaseModel):
    ai_host_enabled: bool
    llm_loaded: bool
    tts_loaded: bool
    load_time_seconds: float | None
    load_error: str | None
    voice_enhancement_enabled: bool
    llm_provider: str
    tts_provider: str
    operational: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/track-intro", response_model=dict[str, Any] | None)
def generate_track_intro_fast(req: TrackIntroRequest):
    """
    Generate a track intro announcement using pre-loaded models.
    Returns instantly without cold-start delay.
    """
    try:
        ai = get_ai_host_fast()
    except Exception as exc:
        _log.error("Fast AI host service unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="AI host service not initialized")

    announcement = ai.generate_track_intro_announcement(
        station_id=req.station_id,
        station_name=req.station_name,
        title=req.title,
        artist=req.artist,
        settings=req.settings,
    )

    if announcement is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to generate announcement (check logs for details)",
        )

    return {
        "cache_key": announcement.cache_key,
        "text": announcement.text,
        "audio_path": announcement.audio_path,
        "duration_seconds": announcement.duration_seconds,
        "llm_provider": announcement.llm_provider,
        "tts_provider": announcement.tts_provider,
        "generated_at": announcement.generated_at,
    }


@router.post("/station-id", response_model=dict[str, Any] | None)
def generate_station_id_fast(req: StationIdRequest):
    """
    Generate a station ID announcement using pre-loaded models.
    Returns instantly without cold-start delay.
    """
    try:
        ai = get_ai_host_fast()
    except Exception as exc:
        _log.error("Fast AI host service unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="AI host service not initialized")

    announcement = ai.generate_station_id_announcement(
        station_id=req.station_id,
        station_name=req.station_name,
        settings=req.settings,
    )

    if announcement is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to generate station ID announcement",
        )

    return {
        "cache_key": announcement.cache_key,
        "text": announcement.text,
        "audio_path": announcement.audio_path,
        "duration_seconds": announcement.duration_seconds,
        "llm_provider": announcement.llm_provider,
        "tts_provider": announcement.tts_provider,
        "generated_at": announcement.generated_at,
    }


@router.post("/tts-test", response_model=TTSTestResponse)
def test_tts_quality(req: TTSTestRequest):
    """
    Test TTS voice quality with custom text and preset.
    Uses edge-tts first (cloud-based, no local model needed), falls back to Qwen TTS.
    """
    try:
        ai = get_ai_host_fast()
    except Exception as exc:
        return TTSTestResponse(
            success=False,
            text_used=req.text,
            preset_used=req.preset,
            error=f"AI host service not initialized: {exc}",
        )

    # Apply voice enhancement
    from app.services.voice_enhancer import enhance_for_tts
    enhanced_text = enhance_for_tts(req.text)

    output_path = ai._audio_path_for_key(f"tts_test_{req.preset}")
    try:
        # Use _synthesize which tries edge-tts first, then Qwen TTS
        tts_provider = ai._synthesize(
            enhanced_text,
            output_path,
            persona=req.preset,
        )

        if tts_provider and output_path.exists():
            from app.audio.audio_processing import probe_duration
            duration = probe_duration(str(output_path))

            return TTSTestResponse(
                success=True,
                audio_path=str(output_path),
                duration_seconds=round(duration, 2) if duration else None,
                text_used=enhanced_text,
                preset_used=req.preset,
            )
        else:
            return TTSTestResponse(
                success=False,
                text_used=enhanced_text,
                preset_used=req.preset,
                error="All TTS providers failed (edge-tts and/or Qwen TTS)",
            )

    except Exception as exc:
        _log.error("TTS test failed: %s", exc, exc_info=True)
        return TTSTestResponse(
            success=False,
            text_used=enhanced_text,
            preset_used=req.preset,
            error=str(exc),
        )


@router.get("/status", response_model=FastStatusResponse)
async def get_fast_status(request: Request):
    """Get the status of the fast AI host service."""
    try:
        ai = get_ai_host_fast()
    except Exception:
        return FastStatusResponse(
            ai_host_enabled=False,
            llm_loaded=False,
            tts_loaded=False,
            load_time_seconds=None,
            load_error="Service not initialized",
            voice_enhancement_enabled=False,
            llm_provider="unavailable",
            tts_provider="unavailable",
            operational=False,
        )

    station_id = request.query_params.get("station_id", 1)
    try:
        settings = SettingsRepository(get_connection()).get_station(int(station_id))
    except Exception:
        settings = {}

    status = ai.get_load_status(settings=settings)

    return FastStatusResponse(
        ai_host_enabled=True,
        llm_loaded=status["llm_loaded"],
        tts_loaded=status["tts_loaded"],
        load_time_seconds=status["load_time_seconds"],
        load_error=status["load_error"],
        voice_enhancement_enabled=status["voice_enhancement_enabled"],
        llm_provider="local-qwen" if status["llm_loaded"] else "template-fallback",
        tts_provider=str(status.get("tts_provider", "unavailable") or "unavailable"),
        operational=bool(status.get("ready", False)),
    )


@router.post("/warmup", response_model=dict[str, Any])
def warmup_models():
    """
    Trigger model warmup (pre-load if not already loaded, then test).
    Useful for background preloading after server start.
    """
    try:
        ai = get_ai_host_fast()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"AI host service unavailable: {exc}")

    result = ai.warmup()
    return result
