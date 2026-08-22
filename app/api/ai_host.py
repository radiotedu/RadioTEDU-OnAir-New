"""AI Host Settings API."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.services.ai_host import DEFAULT_PROMPT_TEMPLATE

router = APIRouter(prefix="/api/ai", tags=["ai"])
_log = logging.getLogger("cleanroom.api.ai")


def _bounded_int(raw, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


class AISettingsPayload(BaseModel):
    station_id: int = 1
    ai_host_enabled: bool = False
    llm_model: str = "Qwen/Qwen2.5-0.5B-Instruct"
    tts_provider: str = "local-qwen-tts"
    tts_model_path: str = ""
    voice_persona: str = "auto"  # auto, morning, afternoon, evening, night
    announcement_max_seconds: int = 15
    include_music_history: bool = True
    educational_segments_enabled: bool = False
    station_id_announcement_interval: int = 1800  # 30 minutes
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE


class AIStatusResponse(BaseModel):
    ai_host_enabled: bool
    llm_loaded: bool
    tts_loaded: bool
    tts_model_exists: bool
    current_persona: str
    cache_size: int
    announcements_generated: int
    llm_provider: str
    tts_provider: str
    operational: bool
    prompt_template_configured: bool


class AIBatchPregenPayload(BaseModel):
    station_id: int = 1
    count: int = 8
    lookahead: int = 8
    track_intros_only: bool = True


@router.get("/settings")
async def get_ai_settings(request: Request):
    """Get AI host settings for the active station."""
    station_id = request.query_params.get("station_id", 1)
    
    conn = None
    try:
        init_db()
        conn = get_connection()
        settings_repo = SettingsRepository(conn)
        station_settings = settings_repo.get_station(int(station_id))
    except Exception as e:
        _log.error("Failed to load AI settings: %s", e)
        station_settings = {}
    finally:
        if conn is not None:
            conn.close()
    
    return {
        "station_id": int(station_id),
        "ai_host_enabled": str(station_settings.get("ai_host_enabled", "false")).lower() in ("1", "true", "yes", "on"),
        "llm_model": station_settings.get("ai_llm_model", "Qwen/Qwen2.5-0.5B-Instruct"),
        "tts_provider": station_settings.get("ai_tts_provider", "local-qwen-tts"),
        "tts_model_path": station_settings.get("ai_tts_model_path", ""),
        "voice_persona": station_settings.get("ai_voice_persona", "auto"),
        "announcement_max_seconds": _bounded_int(
            station_settings.get("ai_announcement_max_seconds", "15"),
            15,
            5,
            120,
        ),
        "include_music_history": str(station_settings.get("ai_include_music_history", "true")).lower() in ("1", "true", "yes", "on"),
        "educational_segments_enabled": str(station_settings.get("ai_educational_segments", "false")).lower() in ("1", "true", "yes", "on"),
        "station_id_announcement_interval": _bounded_int(
            station_settings.get("ai_station_id_interval", "1800"),
            1800,
            60,
            86400,
        ),
        "prompt_template": station_settings.get("ai_prompt_template", DEFAULT_PROMPT_TEMPLATE),
    }


@router.post("/settings")
async def update_ai_settings(payload: AISettingsPayload):
    """Update AI host settings for the active station."""
    try:
        station_id = int(payload.station_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_station_id")

    tts_provider = str(payload.tts_provider or "local-qwen-tts").strip().lower()
    if tts_provider not in {"local-qwen-tts", "windows-sapi", "edge-tts", "omnivoice"}:
        raise HTTPException(
            status_code=422,
            detail=(
                "tts_provider must be local-qwen-tts, windows-sapi, edge-tts, "
                "or omnivoice"
            ),
        )

    settings_map = {
        "ai_host_enabled": str(bool(payload.ai_host_enabled)).lower(),
        "ai_llm_model": str(payload.llm_model),
        "ai_tts_provider": tts_provider,
        "ai_tts_model_path": str(payload.tts_model_path),
        "ai_voice_persona": str(payload.voice_persona),
        "ai_announcement_max_seconds": str(
            _bounded_int(payload.announcement_max_seconds, 15, 5, 120)
        ),
        "ai_include_music_history": str(bool(payload.include_music_history)).lower(),
        "ai_educational_segments": str(bool(payload.educational_segments_enabled)).lower(),
        "ai_station_id_interval": str(
            _bounded_int(
                payload.station_id_announcement_interval, 1800, 60, 86400
            )
        ),
        "ai_prompt_template": str(payload.prompt_template or DEFAULT_PROMPT_TEMPLATE),
    }

    conn = None
    try:
        init_db()
        conn = get_connection()
        settings_repo = SettingsRepository(conn)

        settings_repo.upsert_station(station_id, settings_map)

        purged_pending_announcements = 0
        if not bool(payload.ai_host_enabled):
            from app.services.ai_prefetch import get_ai_prefetch

            get_ai_prefetch().stop(station_id)
            cursor = conn.execute(
                "DELETE FROM queue_items WHERE station_id=? AND status='pending' AND track_id IN ("
                "SELECT id FROM tracks WHERE station_id=? "
                "AND LOWER(COALESCE(track_type, 'music'))='announcement'"
                ")",
                (station_id, station_id),
            )
            purged_pending_announcements = int(cursor.rowcount or 0)
            pending = conn.execute(
                "SELECT id FROM queue_items WHERE station_id=? AND status='pending' "
                "ORDER BY position ASC, id ASC",
                (station_id,),
            ).fetchall()
            for position, row in enumerate(pending, start=1):
                conn.execute(
                    "UPDATE queue_items SET position=? WHERE id=?",
                    (position, int(row["id"])),
                )
            conn.commit()

        _log.info("AI settings updated for station %d", station_id)
        return {"status": "ok", "station_id": station_id}
    except Exception as e:
        _log.error("Failed to update AI settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="failed_to_update_settings")
    finally:
        if conn is not None:
            conn.close()


@router.get("/status")
async def get_ai_status(request: Request):
    """Get AI host runtime status."""
    station_id = request.query_params.get("station_id", 1)
    conn = None
    try:
        from app.services.ai_host import get_ai_host

        conn = get_connection()
        settings_repo = SettingsRepository(conn)
        station_settings = settings_repo.get_station(int(station_id))
        ai = get_ai_host()
        try:
            status = ai.get_status(
                settings=station_settings, station_id=int(station_id)
            )
        except TypeError:
            # Preserve compatibility with lightweight adapters that predate
            # station-scoped cache accounting.
            status = ai.get_status(settings=station_settings)
        ai_enabled = str(station_settings.get("ai_host_enabled", "false")).lower() in ("1", "true", "yes", "on")

        return AIStatusResponse(
            ai_host_enabled=ai_enabled,
            llm_loaded=bool(status.get("llm_loaded", False)),
            tts_loaded=bool(status.get("tts_loaded", False)),
            tts_model_exists=bool(status.get("tts_model_exists", False)),
            current_persona=str(status.get("current_persona", "unknown") or "unknown"),
            cache_size=int(status.get("cache_size", 0) or 0),
            announcements_generated=int(status.get("announcements_generated", 0) or 0),
            llm_provider=str(status.get("llm_provider", "template-fallback") or "template-fallback"),
            tts_provider=str(status.get("tts_provider", "unavailable") or "unavailable"),
            operational=bool(ai_enabled and status.get("ready", False)),
            prompt_template_configured=bool(status.get("prompt_template_configured", False)),
        ).model_dump()
    except Exception as e:
        _log.warning("AI host not initialized: %s", e)
        return AIStatusResponse(
            ai_host_enabled=False,
            llm_loaded=False,
            tts_loaded=False,
            tts_model_exists=False,
            current_persona="unknown",
            cache_size=0,
            announcements_generated=0,
            llm_provider="template-fallback",
            tts_provider="unavailable",
            operational=False,
            prompt_template_configured=False,
        ).model_dump()
    finally:
        if conn is not None:
            conn.close()


@router.post("/warmup")
async def warmup_ai_models(request: Request):
    """Pre-load AI models (warmup)."""
    try:
        from app.services.ai_host import get_ai_host

        station_id = request.query_params.get("station_id", 1)
        station_settings = SettingsRepository(get_connection()).get_station(int(station_id))
        ai = get_ai_host()
        ai.warmup(settings=station_settings)

        status = ai.get_status(settings=station_settings)
        return {
            "status": "ok",
            "message": "AI providers warmed up",
            "llm_provider": status.get("llm_provider", "template-fallback"),
            "tts_provider": status.get("tts_provider", "unavailable"),
        }
    except Exception as e:
        _log.error("AI warmup failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"ai_warmup_failed: {str(e)}")


@router.post("/clear-cache")
async def clear_ai_cache(request: Request):
    """Clear AI announcement cache."""
    try:
        from app.services.ai_host import get_ai_host

        ai = get_ai_host()
        ai.clear_cache()

        return {"status": "ok", "message": "AI cache cleared"}
    except Exception as e:
        _log.error("Failed to clear AI cache: %s", e)
        raise HTTPException(status_code=500, detail="failed_to_clear_cache")


@router.post("/batch-pregen")
async def batch_pregen_ai_announcements(payload: AIBatchPregenPayload):
    """Generate a batch of upcoming AI announcements for the station queue."""
    try:
        from app.services.ai_prefetch import get_ai_prefetch

        station_id = int(payload.station_id)
        count = max(1, int(payload.count))
        lookahead = max(1, int(payload.lookahead))

        service = get_ai_prefetch()
        result = service.batch_generate_station(
            station_id,
            max_generate=count,
            lookahead=lookahead,
            track_intros_only=bool(payload.track_intros_only),
        )
        return {"status": "ok", **result}
    except Exception as e:
        _log.error("AI batch pregen failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"ai_batch_pregen_failed: {str(e)}")


@router.get("/announcements")
async def list_announcements(request: Request):
    """List cached AI announcements."""
    try:
        from app.services.ai_host import get_ai_host

        station_id_raw = request.query_params.get("station_id")
        station_id = int(station_id_raw) if station_id_raw not in (None, "") else None
        ai = get_ai_host()
        announcements = ai.list_cached_announcements(station_id=station_id)
        return {
            "total": len(announcements),
            "announcements": announcements,
        }
    except Exception as e:
        _log.error("Failed to list announcements: %s", e)
        return {"total": 0, "announcements": []}
