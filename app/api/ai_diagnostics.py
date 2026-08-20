"""
AI Diagnostics API.

Provides system information, performance metrics, and diagnostic tools
for the AI host service.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

_log = logging.getLogger("cleanroom.api.ai_diagnostics")

router = APIRouter(prefix="/api/ai", tags=["AI Diagnostics"])


def _get_model_size(path: Path) -> dict[str, Any]:
    """Get size information for a model directory."""
    if not path.exists():
        return {"exists": False}

    total_size = 0
    file_count = 0
    largest_file = None
    largest_size = 0

    for p in path.rglob("*"):
        if p.is_file():
            size = p.stat().st_size
            total_size += size
            file_count += 1
            if size > largest_size:
                largest_size = size
                largest_file = str(p.relative_to(path))

    return {
        "exists": True,
        "path": str(path),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "total_size_gb": round(total_size / (1024 * 1024 * 1024), 3),
        "file_count": file_count,
        "largest_file": largest_file,
        "largest_file_size_mb": round(largest_size / (1024 * 1024), 2),
    }


def _get_system_info() -> dict[str, Any]:
    """Get system hardware and software information."""
    info: dict[str, Any] = {}

    # OS
    info["os"] = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }

    # Python
    info["python"] = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable if "sys" in globals() else None,
    }

    # CPU
    try:
        import psutil
        info["cpu"] = {
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
        }
    except ImportError:
        info["cpu"] = {"note": "psutil not installed — install with: pip install psutil"}

    # Memory
    try:
        import psutil
        mem = psutil.virtual_memory()
        info["memory"] = {
            "total_gb": round(mem.total / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "percent_used": mem.percent,
        }
    except ImportError:
        info["memory"] = {"note": "psutil not installed"}

    # Disk
    try:
        disk = shutil.disk_usage("/")
        info["disk"] = {
            "total_gb": round(disk.total / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "percent_used": round((disk.total - disk.free) / disk.total * 100, 1),
        }
    except Exception:
        info["disk"] = {"note": "Could not determine disk usage"}

    return info


def _get_python_packages() -> dict[str, str]:
    """Get versions of relevant Python packages."""
    packages = {}
    pkg_names = [
        "torch", "transformers", "accelerate", "soundfile",
        "numpy", "qwen_tts", "fastapi", "uvicorn",
    ]
    for name in pkg_names:
        try:
            mod = __import__(name)
            version = getattr(mod, "__version__", getattr(mod, "VERSION", "unknown"))
            packages[name] = str(version)
        except ImportError:
            packages[name] = "not installed"
    return packages


def _get_ai_model_paths() -> dict[str, Any]:
    """Get AI model path status."""
    base = Path(__file__).resolve().parents[2]
    models_dir = base / "models"

    llm_dir = models_dir / "qwen2.5-0.5b-instruct"
    tts_dir = models_dir / "qwen3-tts-voice-design"

    return {
        "models_dir": str(models_dir),
        "models_dir_exists": models_dir.exists(),
        "llm": _get_model_size(llm_dir),
        "tts": _get_model_size(tts_dir),
        "cache_dir": str(base / "data" / "ai_cache"),
    }


def _get_env_vars() -> dict[str, str]:
    """Get relevant environment variables (safe ones only)."""
    keys = [
        "AI_PRELOAD_MODELS",
        "AI_HOST_ENABLED",
        "CLEANROOM_HOST",
        "CLEANROOM_PORT",
        "PYTHONPATH",
    ]
    result = {}
    for key in keys:
        result[key] = os.getenv(key, "<not set>")
    return result


@router.get("/system-info")
async def get_system_info():
    """
    Get comprehensive system information for AI diagnostics.
    Includes hardware, software, packages, and model paths.
    """
    return {
        "system": _get_system_info(),
        "packages": _get_python_packages(),
        "model_paths": _get_ai_model_paths(),
        "environment": _get_env_vars(),
    }


@router.get("/model-status")
async def get_model_status():
    """
    Get the current status of AI models (loaded, available, paths, sizes).
    """
    from app.services.ai_host import (
        DEFAULT_LLM_LOCAL_DIR,
        DEFAULT_TTS_LOCAL_DIR,
        MODELS_DIR,
        CACHE_DIR,
    )

    llm_info = _get_model_size(DEFAULT_LLM_LOCAL_DIR)
    tts_info = _get_model_size(DEFAULT_TTS_LOCAL_DIR)

    # Check fast service status
    fast_loaded = False
    fast_error = None
    try:
        from app.services.ai_host_fast import get_ai_host_fast
        ai_fast = get_ai_host_fast()
        status = ai_fast.get_load_status()
        fast_loaded = status["llm_loaded"] or status["tts_loaded"]
        fast_error = status.get("load_error")
    except Exception as exc:
        fast_error = str(exc)

    # Check original service status
    orig_loaded = False
    try:
        from app.services.ai_host import get_ai_host
        ai_orig = get_ai_host()
        orig_loaded = ai_orig._llm_loaded or ai_orig._tts_loaded
    except Exception:
        pass

    return {
        "llm_model": llm_info,
        "tts_model": tts_info,
        "cache_dir": str(CACHE_DIR),
        "cache_dir_exists": CACHE_DIR.exists(),
        "fast_service": {
            "loaded": fast_loaded,
            "error": fast_error,
        },
        "original_service": {
            "loaded": orig_loaded,
        },
        "models_dir": str(MODELS_DIR),
    }


@router.post("/benchmark")
async def run_benchmark():
    """
    Run a quick AI benchmark to measure generation performance.
    Tests both LLM text generation and TTS synthesis speed.
    """
    import time

    results: dict[str, Any] = {"success": True, "benchmarks": {}}

    # LLM Benchmark
    try:
        from app.services.ai_host_fast import get_ai_host_fast
        ai = get_ai_host_fast()

        if ai._llm_loaded:
            # Warmup
            ai._generate_text("Say hi.", max_tokens=5)

            # Timed generation
            start = time.monotonic()
            text = ai._generate_text(
                "Write a short radio intro for a classical music piece.",
                max_tokens=64,
            )
            elapsed = round((time.monotonic() - start) * 1000, 2)

            results["benchmarks"]["llm"] = {
                "status": "ok",
                "generation_time_ms": elapsed,
                "output_length": len(text) if text else 0,
                "output_preview": text[:100] if text else "(empty)",
            }
        else:
            results["benchmarks"]["llm"] = {"status": "not_loaded"}

    except Exception as exc:
        results["benchmarks"]["llm"] = {"status": "error", "error": str(exc)}

    # TTS Benchmark
    try:
        from app.services.ai_host_fast import get_ai_host_fast
        from app.services.ai_host import CACHE_DIR
        ai = get_ai_host_fast()

        if ai._tts_loaded:
            test_text = "This is a performance benchmark test."
            test_path = CACHE_DIR / "benchmark_test.wav"

            start = time.monotonic()
            ok = ai._synthesize_with_local_tts(
                test_text,
                test_path,
                persona="warm_radio_host",
            )
            elapsed = round((time.monotonic() - start) * 1000, 2)

            if ok and test_path.exists():
                from app.audio.audio_processing import probe_duration
                duration = probe_duration(str(test_path))
                test_path.unlink(missing_ok=True)

                results["benchmarks"]["tts"] = {
                    "status": "ok",
                    "synthesis_time_ms": elapsed,
                    "audio_duration_seconds": round(duration, 2) if duration else None,
                    "realtime_factor": round((duration * 1000) / elapsed, 3) if duration else None,
                }
            else:
                results["benchmarks"]["tts"] = {"status": "failed", "note": "No audio generated"}

        else:
            results["benchmarks"]["tts"] = {"status": "not_loaded"}

    except Exception as exc:
        results["benchmarks"]["tts"] = {"status": "error", "error": str(exc)}

    return results
