"""
Fast AI Host Service — pre-loaded model service.

This service loads the LLM and TTS models at initialization time, keeping them
resident in memory for instant request handling. It is a drop-in replacement
for the lazy-loading AIHostService, designed for production use where cold-start
delays are unacceptable.

The original ai_host.py service remains available as a fallback.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.services.voice_presets import get_instruct_prompt, get_preset
from app.services.voice_enhancer import enhance_for_tts

_log = logging.getLogger("cleanroom.ai_host_fast")

# Import the original service for fallback and shared logic
from app.services.ai_host import (
    AIHostService,
    CACHE_DIR,
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_LOCAL_DIR,
    DEFAULT_TTS_LOCAL_DIR,
    DEFAULT_PROMPT_TEMPLATE,
    MODELS_DIR,
    AIAnnouncement,
    get_ai_host,
)


class AIHostFastService(AIHostService):
    """
    AI Host Service with pre-loaded models.

    Models are loaded during __init__ (or via explicit preload call) and kept
    resident in memory. This eliminates the 5-15 second cold-start delay of
    the lazy-loading parent class.

    Thread-safe: Uses a lock for model access to prevent concurrent loading.
    """

    def __init__(
        self,
        *,
        preload_models: bool = True,
        llm_model_token: str = "",
        tts_model_path: str = "",
        llm_dtype: str = "auto",  # "auto", "float16", "float32"
        enable_voice_enhancement: bool = True,
    ):
        # Call parent __init__ to initialize base state
        super().__init__()

        self._preload_models = preload_models
        self._llm_model_token = llm_model_token or DEFAULT_LLM_MODEL_ID
        self._tts_model_path = tts_model_path
        self._llm_dtype = llm_dtype
        self._enable_voice_enhancement = enable_voice_enhancement
        self._llm_load_lock = threading.Lock()
        self._tts_load_lock = threading.Lock()
        self._preload_lock = threading.Lock()
        self._load_time_seconds: float | None = None
        self._load_error: str | None = None
        self._background_preload_started = False

        if preload_models:
            self._preload_all()

    @staticmethod
    def _local_tts_request_timeout_seconds() -> float:
        try:
            raw = os.getenv("QWEN_TTS_REQUEST_TIMEOUT_SECONDS", "86400")
            return max(30.0, min(86400.0, float(raw)))
        except (TypeError, ValueError):
            return 86400.0

    # ── Preloading ────────────────────────────────────────────────────────

    def _preload_all(self) -> None:
        """Skip heavy model preloading on memory-constrained machines.

        LLM runs via Ollama (separate process), TTS runs via subprocess script.
        No 3-4 GB models are loaded into this process.
        """
        self._load_time_seconds = 0.0
        self._tts_loaded = True  # TTS available via subprocess
        self._llm_loaded = True  # LLM available via Ollama
        _log.info("AI model preload skipped (Ollama LLM + subprocess TTS)")

    # ── Optimized LLM Loading ─────────────────────────────────────────────

    def _load_llm(self, model_token: str = "") -> bool:
        """LLM is served via Ollama (separate process). Never load transformers in-process."""
        return False

    def start_background_preload(self) -> None:
        """Warm models in the background without blocking app startup."""
        with self._preload_lock:
            if self._background_preload_started:
                return
            self._background_preload_started = True

        thread = threading.Thread(
            target=self._preload_all,
            daemon=True,
            name="ai-host-fast-preload",
        )
        thread.start()

    # ── Optimized TTS Loading ─────────────────────────────────────────────

    def _load_tts(self, tts_model_path: str = "") -> bool:
        """TTS runs in a subprocess. Never load qwen_tts in-process on 8 GB machines."""
        model_dir = self._resolve_tts_dir(tts_model_path)
        if model_dir and model_dir.exists():
            self._tts_model_dir = model_dir
            self._tts_loaded = True
        return self._tts_loaded

    def preload_for_playout(self) -> dict[str, Any]:
        """Mark external providers ready without loading multi-GB models in-process."""
        self._preload_all()
        return self.get_load_status()

    # ── Optimized Text Generation ─────────────────────────────────────────

    def _generate_text(self, prompt: str, max_tokens: int = 96, *, model_token: str = "") -> str:
        """Use Ollama (separate process) instead of in-process transformers."""
        return super()._generate_text(prompt, max_tokens, model_token=model_token)

    # ── Optimized TTS Synthesis with Voice Enhancement ────────────────────

    def _synthesize_with_edge_tts(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
    ) -> bool:
        """Synthesize speech using edge-tts (Microsoft's free cloud TTS)."""
        try:
            import asyncio
            import edge_tts

            # Get edge-tts voice and rate from preset
            preset = get_preset(persona)
            if preset is None:
                from app.services.voice_presets import legacy_persona_to_preset
                preset = legacy_persona_to_preset(persona)

            voice = preset.edge_tts_voice if preset else "en-US-GuyNeural"
            rate = preset.edge_tts_rate if preset else "+0%"

            if self._enable_voice_enhancement:
                text = enhance_for_tts(text)

            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Use edge-tts save method (more reliable than streaming)
            async def _generate():
                communicate = edge_tts.Communicate(text, voice, rate=rate)
                await communicate.save(str(output_path))
                return True

            self._run_async_blocking(_generate())
            return output_path.exists() and output_path.stat().st_size > 1024

        except Exception as exc:
            _log.warning("edge-tts synthesis failed: %s", exc)
            return False

    # ── Persistent TTS Server ─────────────────────────────────────────────

    _tts_server_proc: Any = None
    _tts_server_lock = threading.Lock()
    _tts_server_stdout_queue: Any = None
    _tts_server_model_dir: str = ""

    def _ensure_tts_server(self, tts_model_path: str = ""):
        """Start or return the persistent TTS server process."""
        import subprocess as _sp

        model_dir_path = (
            self._resolve_tts_dir(tts_model_path or self._tts_model_path)
            or DEFAULT_TTS_LOCAL_DIR
        )
        model_dir = str(model_dir_path)

        with self._tts_server_lock:
            proc = self._tts_server_proc
            if (
                proc is not None
                and proc.poll() is None
                and self._tts_server_stdout_queue is not None
                and self._tts_server_model_dir == model_dir
            ):
                return proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._tts_server_proc = None
            self._tts_server_stdout_queue = None
            self._tts_server_model_dir = ""

            python_path = self._omnivoice_python()
            server_script = Path(__file__).parent / "qwen_tts_server.py"
            if python_path is None or not server_script.exists():
                _log.warning("TTS server not launchable: python=%s script=%s", python_path, server_script)
                return None

            try:
                child_env = dict(os.environ)
                child_env.pop("PYTHONPATH", None)
                child_env["QWEN_TTS_MODEL_DIR"] = model_dir
                child_env.setdefault("QWEN_TTS_THREADS", "2")
                child_env.setdefault("TOKENIZERS_PARALLELISM", "false")
                child_env.setdefault("OMP_NUM_THREADS", "2")
                child_env.setdefault("MKL_NUM_THREADS", "2")
                child_env.setdefault("OPENBLAS_NUM_THREADS", "2")
                child_env.setdefault("QWEN_TTS_DTYPE", "float32")
                proc = _sp.Popen(
                    [python_path, str(server_script)],
                    stdin=_sp.PIPE,
                    stdout=_sp.PIPE,
                    stderr=_sp.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                )
                stdout_queue: queue.Queue[str] = queue.Queue()
                stderr_tail: list[str] = []

                def _drain_stdout() -> None:
                    try:
                        assert proc.stdout is not None
                        for raw in proc.stdout:
                            stdout_queue.put(raw)
                    except Exception:
                        pass

                def _drain_stderr() -> None:
                    try:
                        assert proc.stderr is not None
                        for raw in proc.stderr:
                            stderr_tail.append(str(raw or "").rstrip())
                            if len(stderr_tail) > 80:
                                del stderr_tail[:40]
                    except Exception:
                        pass

                threading.Thread(target=_drain_stdout, daemon=True).start()
                threading.Thread(target=_drain_stderr, daemon=True).start()

                ready = False
                start_wait = time.monotonic()
                while proc.poll() is None and (time.monotonic() - start_wait) < 180:
                    try:
                        line = stdout_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if msg.get("ready"):
                        ready = True
                        break

                if not ready:
                    tail = "\n".join(stderr_tail[-10:])
                    _log.warning("TTS server did not become ready in time: %s", tail)
                    proc.kill()
                    return None

                self._tts_server_proc = proc
                self._tts_server_stdout_queue = stdout_queue
                self._tts_server_model_dir = model_dir
                _log.info("TTS server started (PID %d)", proc.pid)
                return proc
            except Exception as exc:
                _log.warning("Failed to start TTS server: %s", exc)
                return None

    def _synthesize_with_local_tts(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
        tts_model_path: str = "",
    ) -> bool:
        """Send a synthesis request to the persistent TTS server."""
        import subprocess as _sp

        proc = self._ensure_tts_server(tts_model_path)
        if proc is None or proc.poll() is not None:
            return self._synthesize_with_local_tts_cli(
                text,
                output_path,
                persona=persona,
                tts_model_path=tts_model_path,
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        request = json.dumps({
            "text": enhance_for_tts(text),
            "output_path": str(output_path),
            "persona": persona or "warm_friend",
            "instruct": get_instruct_prompt(persona or "warm_radio_host"),
            "model_dir": tts_model_path or str(self._resolve_tts_dir(tts_model_path) or ""),
            "language": "English",
        })

        try:
            with self._tts_server_lock:
                proc.stdin.write(request + "\n")
                proc.stdin.flush()
                stdout_queue = self._tts_server_stdout_queue
                line = ""
                timeout_seconds = self._local_tts_request_timeout_seconds()
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        line = stdout_queue.get(timeout=1.0) if stdout_queue is not None else ""
                    except queue.Empty:
                        continue
                    if line.strip():
                        break
            if not line.strip():
                _log.warning("TTS server synthesis timed out after %.0fs", timeout_seconds)
                try:
                    proc.kill()
                except Exception:
                    pass
                self._tts_server_proc = None
                self._tts_server_stdout_queue = None
                return False
            result = json.loads(line.strip())
            if result.get("ok"):
                _log.info("TTS server synthesis OK: %s (%.1fs)", output_path, result.get("synth_seconds", 0))
                return True
            _log.warning("TTS server synthesis failed: %s", result.get("error"))
            self._tts_server_proc = None
            self._tts_server_stdout_queue = None
            return self._synthesize_with_local_tts_cli(
                text,
                output_path,
                persona=persona,
                tts_model_path=tts_model_path,
            )
        except Exception as exc:
            _log.warning("TTS server error: %s", exc)
            self._tts_server_proc = None
            self._tts_server_stdout_queue = None
            return self._synthesize_with_local_tts_cli(
                text,
                output_path,
                persona=persona,
                tts_model_path=tts_model_path,
            )

    def _synthesize_with_local_tts_cli(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
        tts_model_path: str = "",
    ) -> bool:
        """Fallback path: run the one-shot Qwen helper when the server is unavailable."""
        import subprocess as _sp

        python_path = self._omnivoice_python()
        script_path = Path(__file__).parent / "qwen_tts_cli.py"
        if python_path is None or not script_path.exists():
            _log.warning("Qwen TTS CLI fallback not launchable: python=%s script=%s", python_path, script_path)
            return False

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": enhance_for_tts(text),
            "output_path": str(output_path),
            "persona": persona or "warm_friend",
            "instruct": get_instruct_prompt(persona or "warm_radio_host"),
            "model_dir": tts_model_path or str(self._resolve_tts_dir(tts_model_path) or ""),
            "language": "English",
        }
        child_env = dict(os.environ)
        child_env.pop("PYTHONPATH", None)
        child_env.setdefault("QWEN_TTS_THREADS", "2")
        child_env.setdefault("TOKENIZERS_PARALLELISM", "false")
        child_env.setdefault("OMP_NUM_THREADS", "2")
        child_env.setdefault("MKL_NUM_THREADS", "2")
        child_env.setdefault("OPENBLAS_NUM_THREADS", "2")
        child_env.setdefault("QWEN_TTS_DTYPE", "float32")
        if payload.get("model_dir"):
            child_env["QWEN_TTS_MODEL_DIR"] = str(payload["model_dir"])

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)

        try:
            proc = _sp.run(
                [str(python_path), str(script_path)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._local_tts_request_timeout_seconds(),
                env=child_env,
                creationflags=creationflags,
            )
            line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            result = json.loads(line) if line else {}
            if proc.returncode == 0 and result.get("ok") and output_path.exists():
                _log.info("Qwen TTS CLI fallback synthesis OK: %s", output_path)
                return True
            _log.warning(
                "Qwen TTS CLI fallback failed (code=%s): %s %s",
                proc.returncode,
                result.get("error", ""),
                (proc.stderr or "")[-500:],
            )
            return False
        except Exception as exc:
            _log.warning("Qwen TTS CLI fallback error: %s", exc)
            return False

    def _synthesize(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
        tts_model_path: str = "",
        settings: dict[str, Any] | None = None,
    ) -> str:
        provider = self._configured_tts_provider(settings)
        if provider == "windows-sapi":
            if self._synthesize_with_windows_sapi(
                text,
                output_path,
                persona=persona,
            ):
                return "windows-sapi"
            return ""

        if provider == "omnivoice":
            if self._synthesize_with_omnivoice(
                text,
                output_path,
                persona=persona,
                settings=settings,
            ):
                return "omnivoice"
            return ""

        if provider == "local-qwen-tts":
            if self._synthesize_with_local_tts(
                text,
                output_path,
                persona=persona,
                tts_model_path=tts_model_path,
            ):
                return "local-qwen-tts"
            return ""

        if provider == "edge-tts":
            if self._synthesize_with_edge_tts(
                text,
                output_path,
                persona=persona,
            ):
                return "edge-tts"
            return ""

        return ""

    # ── Override generation methods to use enhanced text ──────────────────

    def generate_track_intro_announcement(
        self,
        *,
        station_id: int = 1,
        station_name: str = "Main Radio",
        title: str,
        artist: str = "",
        settings: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> Optional[AIAnnouncement]:
        # Use parent class logic but with enhanced text processing
        return super().generate_track_intro_announcement(
            station_id=station_id,
            station_name=station_name,
            title=title,
            artist=artist,
            settings=settings,
            dedupe_key=dedupe_key,
        )

    def generate_station_id_announcement(
        self,
        *,
        station_id: int = 1,
        station_name: str = "Main Radio",
        settings: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> Optional[AIAnnouncement]:
        return super().generate_station_id_announcement(
            station_id=station_id,
            station_name=station_name,
            settings=settings,
            dedupe_key=dedupe_key,
        )

    # ── Diagnostics ───────────────────────────────────────────────────────

    def get_load_status(self, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Get detailed load status of the fast service."""
        provider = self._configured_tts_provider(settings)
        edge_available = self._edge_tts_available()
        if provider == "windows-sapi":
            sapi_available = self._windows_sapi_available()
            tts_provider = "windows-sapi" if sapi_available else "windows-sapi-unavailable"
            tts_loaded = sapi_available
        elif provider == "edge-tts":
            tts_provider = "edge-tts" if edge_available else "edge-tts-unavailable"
            tts_loaded = edge_available
        elif provider == "omnivoice":
            omnivoice_available = self._omnivoice_available()
            tts_provider = "omnivoice" if omnivoice_available else "omnivoice-unavailable"
            tts_loaded = omnivoice_available
        else:
            tts_provider = self._tts_provider_status(self._tts_model_path)
            tts_loaded = self._tts_loaded
        return {
            "llm_loaded": self._llm_loaded,
            "tts_loaded": tts_loaded,
            "edge_tts_available": edge_available,
            "llm_model_token": self._llm_model_token,
            "tts_model_path": self._tts_model_path,
            "llm_dtype": self._llm_dtype,
            "voice_enhancement_enabled": self._enable_voice_enhancement,
            "load_time_seconds": self._load_time_seconds,
            "load_error": self._load_error,
            "preload_requested": self._preload_models,
            "llm_model_dir": str(self._llm_model_dir) if self._llm_model_dir else None,
            "tts_model_dir": str(self._tts_model_dir) if self._tts_model_dir else None,
            "tts_provider": tts_provider,
            "ready": self.is_ready(settings=settings),
        }

    def warmup(self, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Run a warmup generation to ensure models are ready.
        This runs a short text generation to trigger any lazy init inside the models.
        """
        result = {"llm_warmup": False, "tts_warmup": False, "errors": []}

        provider = self._configured_tts_provider(settings)
        # Discover local TTS only when it is the configured provider.
        if provider == "local-qwen-tts" and not self._tts_loaded:
            self._load_tts(self._tts_model_path)

        # Optionally trigger LLM loading (may fail on low-memory systems)
        if not self._llm_loaded:
            try:
                self._load_llm(self._llm_model_token)
            except Exception:
                pass  # LLM is optional

        # TTS warmup
        if self.is_ready(settings=settings):
            try:
                test_path = CACHE_DIR / "warmup_test.wav"
                used_provider = self._synthesize(
                    "Hello, this is a test.",
                    test_path,
                    persona="warm_radio_host",
                    tts_model_path=self._tts_model_path,
                    settings=settings,
                )
                result["tts_warmup"] = bool(used_provider)
                if used_provider and test_path.exists():
                    test_path.unlink()  # Clean up
            except Exception as exc:
                result["errors"].append(f"TTS warmup failed: {exc}")
        # LLM warmup
        if self._llm_loaded:
            try:
                test_text = self._generate_text("Say hello.", max_tokens=10)
                result["llm_warmup"] = bool(test_text)
                if not test_text:
                    result["errors"].append("LLM warmup generation returned empty text")
            except Exception as exc:
                result["errors"].append(f"LLM warmup failed: {exc}")

        return result


# ── Singleton Pattern ────────────────────────────────────────────────────────

_instance: Optional[AIHostFastService] = None
_instance_lock = threading.Lock()


def get_ai_host_fast() -> AIHostFastService:
    """Get or create the fast AI host service singleton."""
    global _instance
    with _instance_lock:
        if _instance is None:
            preload = _should_preload_models()
            # Singleton creation stays non-blocking. Live playout startup owns any
            # synchronous preload so worker ticks do not race a background loader.
            _log.info(
                "Creating AIHostFastService (requested_preload=%s, applied_preload=%s)",
                preload,
                False,
            )
            _instance = AIHostFastService(preload_models=False)
        return _instance


def reset_ai_host_fast() -> None:
    """Reset the fast service singleton (for testing)."""
    global _instance
    with _instance_lock:
        _instance = None


def _should_preload_models() -> bool:
    """Check environment variable for preload setting."""
    import os
    raw = os.getenv("AI_PRELOAD_MODELS", "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True
