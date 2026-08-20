"""
Operational AI host service.

The service uses local Qwen models for text generation and speech synthesis.
If TTS models are not available, announcements will fail.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.audio.audio_processing import probe_duration
from app.runtime_paths import get_data_dir
from app.services.music_history import MusicHistoryDB
from app.services.voice_enhancer import enhance_for_tts
from app.services.voice_presets import get_instruct_prompt

_log = logging.getLogger("cleanroom.ai_host")

BASE_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = BASE_DIR.parent
MODELS_DIR = BASE_DIR / "models"
CACHE_DIR = get_data_dir() / "ai_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_CLEAR_MARKER = CACHE_DIR / ".cleared_at"

DEFAULT_LLM_MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_LLM_LOCAL_DIR = MODELS_DIR / "qwen2.5-0.5b-instruct"
DEFAULT_TTS_LOCAL_DIR = MODELS_DIR / "qwen3-tts-0.6b-customvoice"
DEFAULT_OMNIVOICE_MODEL_ID = "k2-fsa/OmniVoice"
DEFAULT_OMNIVOICE_ENV_DIR = BASE_DIR / "app" / ".venv-omnivoice"
DEFAULT_OMNIVOICE_LOCK_DIR = CACHE_DIR / "omnivoice_generation.lock"
DEFAULT_PROMPT_TEMPLATE = (
    "Keep the intro short, natural, and varied. Use the artist or composer and "
    "a listener-friendly title, not the full metadata title.{history_line}{educational_line}"
)
DEFAULT_SPOKEN_FALLBACK_TEMPLATE = (
    "Now playing {track_title}{artist_phrase} on {station_name}."
    "{history_line}{educational_line}"
)
DEFAULT_STATION_ID_TEMPLATE = "This is {station_name}, live on air."
_SPEECH_WORDS_PER_SECOND = 2.6

# Voice personas — backward-compatible mapping to preset instruct prompts.
# These are kept for legacy support; new code should use voice_presets.py.
VOICE_PERSONAS = {
    "morning": "energetic_morning",
    "afternoon": "warm_radio_host",
    "evening": "smooth_evening",
    "night": "smooth_evening",
}


def get_current_persona() -> str:
    """Get voice persona based on time of day."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def get_time_greeting() -> str:
    """Get time-based greeting for the current hour."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 21:
        return "Good evening"
    return "Good night"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


@dataclass(slots=True)
class AIAnnouncement:
    cache_key: str
    station_id: int
    station_name: str
    announcement_type: str
    title: str
    artist: str
    text: str
    audio_path: str
    duration_seconds: float
    llm_provider: str
    tts_provider: str
    generated_at: str
    dedupe_key: str = ""  # Deduplication key for prefetch lookup

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


class _SafeTemplateDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


class AIHostService:
    """
    AI host that generates announcements using local or explicitly configured providers.

    Text generation provider order:
    1. Local Qwen LLM when available
    2. Built-in template rendering

    Speech synthesis is provider-explicit. Windows SAPI is the lightweight,
    offline production fallback when private Qwen or OmniVoice assets are absent.
    """

    def __init__(self):
        self._llm = None
        self._tts = None
        self._llm_tokenizer = None
        self._llm_loaded = False
        self._tts_loaded = False
        self._llm_model_dir: Path | None = None
        self._tts_model_dir: Path | None = None
        self._announcement_cache: dict[str, AIAnnouncement] = {}
        self._generated_count = 0
        self._history_db: MusicHistoryDB | None = None
        self._powershell_bin: str | None = None
        self._windows_sapi_state: bool | None = None

    def _history(self) -> MusicHistoryDB:
        if self._history_db is None:
            self._history_db = MusicHistoryDB()
        return self._history_db

    @staticmethod
    def _normalize_model_token(raw: str, *, default: str) -> str:
        token = str(raw or "").strip()
        return token or default

    def _resolve_llm_dir(self, model_token: str = "") -> Path | None:
        token = str(model_token or "").strip()
        if not token:
            return DEFAULT_LLM_LOCAL_DIR if DEFAULT_LLM_LOCAL_DIR.exists() else None
        lowered = token.lower()
        if lowered in {"template", "builtin", "fallback", "none", "off"}:
            return None
        candidate = Path(token)
        if candidate.is_dir():
            return candidate
        if lowered in {
            DEFAULT_LLM_MODEL_ID.lower(),
            DEFAULT_LLM_LOCAL_DIR.name.lower(),
        }:
            return DEFAULT_LLM_LOCAL_DIR if DEFAULT_LLM_LOCAL_DIR.exists() else None
        slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
        if slug:
            maybe = MODELS_DIR / slug
            if maybe.exists():
                return maybe
        return None

    def _resolve_tts_dir(self, tts_model_path: str = "") -> Path | None:
        token = str(tts_model_path or "").strip()
        if token.lower() in {"none", "off"}:
            return None
        if token:
            candidate = Path(token)
            if candidate.is_dir():
                return candidate
        if DEFAULT_TTS_LOCAL_DIR.exists():
            return DEFAULT_TTS_LOCAL_DIR
        return None

    def _powershell(self) -> str | None:
        if self._powershell_bin is not None:
            return self._powershell_bin
        self._powershell_bin = shutil.which("powershell") or shutil.which("pwsh")
        return self._powershell_bin

    def _llm_provider_status(self, model_token: str = "") -> str:
        ollama_model = self._ollama_model_name(model_token)
        if ollama_model:
            return "ollama" if self._ollama_available(ollama_model) else "ollama-unavailable"
        if self._llm_loaded:
            return "local-qwen"
        if self._resolve_llm_dir(model_token):
            return "local-qwen-available"
        return "template-fallback"

    @staticmethod
    def _ollama_model_name(model_token: str = "") -> str:
        token = str(model_token or "").strip()
        if not token.lower().startswith("ollama:"):
            return ""
        return token.split(":", 1)[1].strip() or "qwen2.5:0.5b"

    def _ollama_available(self, model_name: str = "") -> bool:
        try:
            import urllib.request

            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if not model_name:
                return True
            wanted = str(model_name or "").strip().lower()
            names = {
                str(item.get("name", "") or "").strip().lower()
                for item in payload.get("models", [])
                if isinstance(item, dict)
            }
            return wanted in names or f"{wanted}:latest" in names
        except Exception:
            return False

    def _tts_provider_status(self, tts_model_path: str = "") -> str:
        if self._tts_loaded:
            return "local-qwen-tts"
        if self._resolve_tts_dir(tts_model_path):
            return "local-qwen-tts-available"
        return "unavailable"

    @staticmethod
    def _configured_tts_provider(settings: dict[str, Any] | None) -> str:
        raw = str((settings or {}).get("ai_tts_provider", "local-qwen-tts") or "").strip().lower()
        normalized = raw.replace("_", "-")
        aliases = {
            "qwen": "local-qwen-tts",
            "qwen-tts": "local-qwen-tts",
            "local-qwen": "local-qwen-tts",
            "local-qwen-tts": "local-qwen-tts",
            "edge": "edge-tts",
            "edge-tts": "edge-tts",
            "sapi": "windows-sapi",
            "windows": "windows-sapi",
            "windows-sapi": "windows-sapi",
            "omnivoice": "omnivoice",
            "omni-voice": "omnivoice",
        }
        return aliases.get(normalized, "local-qwen-tts")

    def _windows_sapi_available(self) -> bool:
        if self._windows_sapi_state is not None:
            return self._windows_sapi_state
        powershell = self._powershell()
        if os.name != "nt" or not powershell:
            self._windows_sapi_state = False
            return False
        probe = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$ok=@($s.GetInstalledVoices()|Where-Object{$_.Enabled}).Count -gt 0;"
            "$s.Dispose();"
            "if($ok){[Console]::Out.Write('ok')}else{exit 2}"
        )
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=creationflags,
            )
            self._windows_sapi_state = (
                completed.returncode == 0 and str(completed.stdout or "").strip() == "ok"
            )
        except Exception as exc:
            _log.warning("Windows SAPI availability probe failed: %s", exc)
            self._windows_sapi_state = False
        return self._windows_sapi_state

    @staticmethod
    def _omnivoice_python() -> Path | None:
        candidates = [
            DEFAULT_OMNIVOICE_ENV_DIR / "Scripts" / "python.exe",
        ]
        if not bool(getattr(sys, "frozen", False)):
            candidates.append(Path(sys.executable))
        for python_path in candidates:
            if python_path.exists():
                return python_path
        found = shutil.which("python") or shutil.which("python3")
        return Path(found) if found else None

    @staticmethod
    def _omnivoice_script_path() -> Path:
        return BASE_DIR / "app" / "services" / "omnivoice_cli.py"

    def _omnivoice_available(self) -> bool:
        python_path = self._omnivoice_python()
        return bool(python_path is not None and self._omnivoice_script_path().exists())

    @staticmethod
    def _omnivoice_instruct(persona: str) -> str:
        mapping = {
            "morning": "male, young adult, moderate pitch, american accent",
            "afternoon": "male, middle-aged, moderate pitch, american accent",
            "evening": "male, middle-aged, low pitch, american accent",
            "night": "male, middle-aged, low pitch, american accent",
            "warm_radio_host": "male, middle-aged, moderate pitch, american accent",
            "professional_announcer": "male, middle-aged, low pitch, british accent",
            "smooth_evening": "male, middle-aged, low pitch, british accent",
            "classical_host": "male, middle-aged, low pitch, british accent",
            "energetic_morning": "male, young adult, moderate pitch, american accent",
            "casual_friend": "male, young adult, moderate pitch, american accent",
        }
        return mapping.get(
            str(persona or "").strip().lower(),
            "male, middle-aged, moderate pitch, american accent",
        )

    @staticmethod
    def _omnivoice_speed(persona: str) -> float:
        mapping = {
            "morning": 1.08,
            "afternoon": 1.0,
            "evening": 0.94,
            "night": 0.92,
            "energetic_morning": 1.08,
            "warm_radio_host": 1.0,
            "professional_announcer": 0.95,
            "smooth_evening": 0.92,
            "classical_host": 0.94,
        }
        return float(mapping.get(str(persona or "").strip().lower(), 1.0))

    @staticmethod
    def _edge_tts_available() -> bool:
        return False

    @staticmethod
    def _run_async_blocking(coro):
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        outcome: dict[str, Any] = {}

        def _runner() -> None:
            try:
                outcome["result"] = asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover - passthrough for async failures
                outcome["error"] = exc

        thread = threading.Thread(target=_runner, daemon=True, name="edge-tts-runner")
        thread.start()
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    def _load_llm(self, model_token: str = "") -> bool:
        if self._llm_loaded:
            return True
        model_dir = self._resolve_llm_dir(model_token)
        if model_dir is None or not model_dir.exists():
            return False
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            _log.info("Loading local AI host LLM from %s", model_dir)
            self._llm_tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
            self._llm = AutoModelForCausalLM.from_pretrained(
                str(model_dir),
                dtype=torch.float32,
                low_cpu_mem_usage=False,
            )
            self._llm.eval()
            self._llm_loaded = True
            self._llm_model_dir = model_dir
            return True
        except Exception as exc:
            _log.warning("AI host LLM load failed: %s", exc)
            return False

    def _render_llm_prompt(self, prompt: Any) -> str:
        if isinstance(prompt, list) and self._llm_tokenizer is not None:
            apply_chat_template = getattr(self._llm_tokenizer, "apply_chat_template", None)
            if callable(apply_chat_template):
                return str(
                    apply_chat_template(
                        prompt,
                        tokenize=False,
                        add_generation_prompt=True,
                    )
                )
        return str(prompt or "")

    def _generate_text_with_ollama(self, prompt: Any, max_tokens: int, *, model_name: str) -> str:
        try:
            import urllib.request

            payload: dict[str, Any]
            endpoint = "http://127.0.0.1:11434/api/generate"
            if isinstance(prompt, list):
                endpoint = "http://127.0.0.1:11434/api/chat"
                payload = {
                    "model": model_name,
                    "messages": prompt,
                    "stream": False,
                    "options": {"num_predict": int(max_tokens), "temperature": 0.75},
                }
            else:
                payload = {
                    "model": model_name,
                    "prompt": self._render_llm_prompt(prompt),
                    "stream": False,
                    "options": {"num_predict": int(max_tokens), "temperature": 0.75},
                }
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30.0) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
            if endpoint.endswith("/chat"):
                return str((result.get("message") or {}).get("content") or "").strip()
            return str(result.get("response") or "").strip()
        except Exception as exc:
            _log.warning("AI host Ollama generation failed: %s", exc)
            return ""

    def _generate_text(self, prompt: Any, max_tokens: int = 96, *, model_token: str = "") -> str:
        ollama_model = self._ollama_model_name(model_token)
        if ollama_model:
            return self._generate_text_with_ollama(prompt, max_tokens, model_name=ollama_model)
        if not self._load_llm(model_token):
            return ""
        try:
            import torch
            prompt_text = self._render_llm_prompt(prompt)
            inputs = self._llm_tokenizer(prompt_text, return_tensors="pt").to(self._llm.device)
            with torch.no_grad():
                outputs = self._llm.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    do_sample=True,
                    temperature=0.85,
                    top_p=0.92,
                )
            new_tokens = outputs[0][inputs["input_ids"].shape[1]:]
            text = self._llm_tokenizer.decode(new_tokens, skip_special_tokens=True)
            return str(text or "").strip()
        except Exception as exc:
            _log.warning("AI host LLM generation failed: %s", exc)
            return ""
        finally:
            gc.collect()

    def _load_tts(self, tts_model_path: str = "") -> bool:
        if self._tts_loaded:
            return True
        model_dir = self._resolve_tts_dir(tts_model_path)
        if model_dir is None or not model_dir.exists():
            return False
        try:
            from qwen_tts import Qwen3TTSModel

            _log.info("Loading local AI host TTS from %s", model_dir)
            self._tts = Qwen3TTSModel.from_pretrained(
                str(model_dir),
                device_map="cpu",
                dtype=torch.bfloat16,
                low_cpu_mem_usage=True,
            )
            self._tts_loaded = True
            self._tts_model_dir = model_dir
            return True
        except Exception as exc:
            _log.warning("AI host TTS load failed: %s", exc)
            return False

    def _synthesize_with_edge_tts(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
    ) -> bool:
        """Synthesize speech using edge-tts (Microsoft's free cloud TTS)."""
        import asyncio
        import edge_tts

        # Get edge-tts voice and rate from preset
        from app.services.voice_presets import get_preset
        preset = get_preset(persona)
        if preset is None:
            from app.services.voice_presets import legacy_persona_to_preset
            preset = legacy_persona_to_preset(persona)

        voice = preset.edge_tts_voice if preset else "en-US-GuyNeural"
        rate = preset.edge_tts_rate if preset else "+0%"

        # Apply voice enhancement
        text = enhance_for_tts(text)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        voices = [voice]
        for fallback_voice in ("en-US-GuyNeural", "en-US-ChristopherNeural"):
            if fallback_voice not in voices:
                voices.append(fallback_voice)

        # edge-tts occasionally returns an empty stream for a specific voice or on
        # the first request. Retry once, then fall back to a more stable voice.
        last_error: Exception | None = None
        for candidate_voice in voices:
            attempts = 2 if candidate_voice == voice else 1
            for attempt in range(attempts):
                try:
                    async def _generate():
                        communicate = edge_tts.Communicate(text, candidate_voice, rate=rate)
                        await communicate.save(str(output_path))
                        return True

                    self._run_async_blocking(_generate())
                    if output_path.exists() and output_path.stat().st_size > 1024:
                        return True
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        time.sleep(0.5)
                        continue
                    break
        if last_error is not None:
            _log.warning("edge-tts synthesis failed: %s", last_error)
        return False

    def _synthesize_with_windows_sapi(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
    ) -> bool:
        """Create a local PCM WAV with an installed Windows voice.

        Values cross the process boundary as base64 environment variables so
        announcement text and file paths are never interpreted as PowerShell.
        """
        if not self._windows_sapi_available():
            return False
        powershell = self._powershell()
        if not powershell:
            return False

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        spoken_text = enhance_for_tts(text)
        if not spoken_text.strip():
            return False
        rate_by_persona = {
            "morning": 0,
            "afternoon": 0,
            "evening": -1,
            "night": -1,
        }
        rate = rate_by_persona.get(str(persona or "").strip().lower(), 0)
        child_env = dict(os.environ)
        child_env["RADIOTEDU_SAPI_TEXT_B64"] = base64.b64encode(
            spoken_text.encode("utf-8")
        ).decode("ascii")
        child_env["RADIOTEDU_SAPI_OUTPUT_B64"] = base64.b64encode(
            str(output_path).encode("utf-8")
        ).decode("ascii")
        child_env["RADIOTEDU_SAPI_RATE"] = str(rate)
        command = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Speech;"
            "$u=[System.Text.Encoding]::UTF8;"
            "$text=$u.GetString([Convert]::FromBase64String($env:RADIOTEDU_SAPI_TEXT_B64));"
            "$out=$u.GetString([Convert]::FromBase64String($env:RADIOTEDU_SAPI_OUTPUT_B64));"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Rate=[int]$env:RADIOTEDU_SAPI_RATE;"
            "$s.SetOutputToWaveFile($out);"
            "$s.Speak($text);"
            "$s.Dispose()"
        )
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=120,
                env=child_env,
                creationflags=creationflags,
            )
        except Exception as exc:
            _log.warning("Windows SAPI synthesis failed: %s", exc)
            return False
        if completed.returncode != 0:
            _log.warning(
                "Windows SAPI synthesis returned %s: %s",
                completed.returncode,
                str(completed.stderr or "").strip()[-1000:],
            )
            return False
        return output_path.exists() and output_path.stat().st_size > 1024

    def _qwen_tts_cli_path(self) -> Path:
        return BASE_DIR / "app" / "services" / "qwen_tts_cli.py"

    def _synthesize_with_local_tts(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
        tts_model_path: str = "",
    ) -> bool:
        python_path = self._omnivoice_python()
        script_path = self._qwen_tts_cli_path()
        if python_path is None or not script_path.exists():
            _log.warning("Qwen TTS CLI not found: python=%s script=%s", python_path, script_path)
            return False

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        instruct = get_instruct_prompt(persona)
        payload = {
            "text": enhance_for_tts(text),
            "output_path": str(output_path),
            "instruct": instruct,
            "model_dir": tts_model_path or str(self._resolve_tts_dir(tts_model_path) or ""),
            "language": "English",
        }
        try:
            child_env = dict(os.environ)
            child_env.pop("PYTHONPATH", None)
            child_env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            child_env.setdefault("TOKENIZERS_PARALLELISM", "false")
            child_env.setdefault("OMP_NUM_THREADS", "2")
            child_env.setdefault("MKL_NUM_THREADS", "2")
            child_env.setdefault("OPENBLAS_NUM_THREADS", "2")
            child_env.setdefault("QWEN_TTS_THREADS", "2")
            child_env.setdefault("QWEN_TTS_DTYPE", "float32")
            if payload.get("model_dir"):
                child_env["QWEN_TTS_MODEL_DIR"] = str(payload["model_dir"])
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            completed = subprocess.run(
                [str(python_path), str(script_path)],
                input=json.dumps(payload, ensure_ascii=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT_DIR),
                timeout=900,
                env=child_env,
                creationflags=creationflags,
            )
        except Exception as exc:
            _log.warning("Qwen TTS subprocess failed: %s", exc)
            return False

        output_lines = [line.strip() for line in str(completed.stdout or "").splitlines() if line.strip()]
        if not output_lines:
            stderr_tail = str(completed.stderr or "").strip()[-1000:]
            _log.warning("Qwen TTS subprocess produced no output: %s", stderr_tail)
            return False
        try:
            result = json.loads(output_lines[-1])
        except json.JSONDecodeError:
            _log.warning("Qwen TTS subprocess output was not JSON: %s", output_lines[-1])
            return False

        if not bool(result.get("ok", False)):
            _log.warning("Qwen TTS synthesis failed: %s", result.get("error", "unknown_error"))
            return False
        return output_path.exists() and output_path.stat().st_size > 1024

    def _synthesize_with_omnivoice(
        self,
        text: str,
        output_path: Path,
        *,
        persona: str,
        settings: dict[str, Any] | None = None,
    ) -> bool:
        python_path = self._omnivoice_python()
        script_path = self._omnivoice_script_path()
        if python_path is None or not script_path.exists():
            return False

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lock_acquired = False
        payload = {
            "text": enhance_for_tts(text),
            "output_path": str(output_path),
            "instruct": self._omnivoice_instruct(persona),
            "speed": self._omnivoice_speed(persona),
            "model_name": str(
                (settings or {}).get("ai_omnivoice_model", DEFAULT_OMNIVOICE_MODEL_ID)
                or DEFAULT_OMNIVOICE_MODEL_ID
            ),
        }
        try:
            try:
                DEFAULT_OMNIVOICE_LOCK_DIR.mkdir(parents=True)
                lock_acquired = True
                (DEFAULT_OMNIVOICE_LOCK_DIR / "owner.json").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "output_path": str(output_path),
                        },
                        ensure_ascii=True,
                    ),
                    encoding="utf-8",
                )
            except FileExistsError:
                try:
                    age_seconds = time.time() - DEFAULT_OMNIVOICE_LOCK_DIR.stat().st_mtime
                except OSError:
                    age_seconds = 0.0
                if age_seconds < 1800:
                    _log.info("OmniVoice generation already in progress; skipping this prefetch cycle")
                    return False
                shutil.rmtree(DEFAULT_OMNIVOICE_LOCK_DIR, ignore_errors=True)
                DEFAULT_OMNIVOICE_LOCK_DIR.mkdir(parents=True)
                lock_acquired = True

            child_env = dict(os.environ)
            child_env.pop("PYTHONPATH", None)
            child_env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            child_env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            child_env.setdefault("TOKENIZERS_PARALLELISM", "false")
            child_env.setdefault("OMNIVOICE_TORCH_THREADS", "4")
            child_env.setdefault("OMP_NUM_THREADS", "4")
            child_env.setdefault("MKL_NUM_THREADS", "4")
            child_env.setdefault("OPENBLAS_NUM_THREADS", "4")
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            completed = subprocess.run(
                [str(python_path), str(script_path)],
                input=json.dumps(payload, ensure_ascii=True),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT_DIR),
                timeout=900,
                env=child_env,
                creationflags=creationflags,
            )
        except Exception as exc:
            _log.warning("OmniVoice subprocess failed: %s", exc)
            return False
        finally:
            if lock_acquired:
                shutil.rmtree(DEFAULT_OMNIVOICE_LOCK_DIR, ignore_errors=True)

        output_lines = [line.strip() for line in str(completed.stdout or "").splitlines() if line.strip()]
        if not output_lines:
            stderr_tail = str(completed.stderr or "").strip()[-1000:]
            _log.warning("OmniVoice subprocess produced no JSON output: %s", stderr_tail)
            return False
        try:
            result = json.loads(output_lines[-1])
        except json.JSONDecodeError:
            _log.warning("OmniVoice subprocess output was not JSON: %s", output_lines[-1])
            return False

        if not bool(result.get("ok", False)):
            _log.warning("OmniVoice synthesis failed: %s", result.get("error", "unknown_error"))
            return False
        return output_path.exists() and output_path.stat().st_size > 1024

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

    @staticmethod
    def _audio_path_for_key(cache_key: str) -> Path:
        return CACHE_DIR / f"announcement_{cache_key}.wav"

    @staticmethod
    def _metadata_path_for_key(cache_key: str) -> Path:
        return CACHE_DIR / f"announcement_{cache_key}.json"

    def _cache_key_for_payload(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _load_cached_announcement(self, cache_key: str) -> AIAnnouncement | None:
        cached = self._announcement_cache.get(cache_key)
        if cached is not None and Path(cached.audio_path).exists():
            return cached

        audio_path = self._audio_path_for_key(cache_key)
        if not audio_path.exists():
            return None

        metadata_path = self._metadata_path_for_key(cache_key)
        payload: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}

        duration = float(payload.get("duration_seconds", 0.0) or 0.0)
        if duration <= 0.0:
            duration = self._duration_seconds(str(audio_path), str(payload.get("text", "")))

        announcement = AIAnnouncement(
            cache_key=cache_key,
            station_id=int(payload.get("station_id", 1) or 1),
            station_name=str(payload.get("station_name", "Main Radio") or "Main Radio"),
            announcement_type=str(payload.get("announcement_type", "announcement") or "announcement"),
            title=str(payload.get("title", "AI Announcement") or "AI Announcement"),
            artist=str(payload.get("artist", "AI Host") or "AI Host"),
            text=str(payload.get("text", "") or ""),
            audio_path=str(audio_path),
            duration_seconds=duration,
            llm_provider=str(payload.get("llm_provider", "template-fallback") or "template-fallback"),
            tts_provider=str(payload.get("tts_provider", "unavailable") or "unavailable"),
            generated_at=str(payload.get("generated_at", "")),
            dedupe_key=str(payload.get("dedupe_key", "") or ""),
        )
        self._announcement_cache[cache_key] = announcement
        return announcement

    def _store_cached_announcement(self, announcement: AIAnnouncement) -> None:
        self._announcement_cache[announcement.cache_key] = announcement
        metadata_path = self._metadata_path_for_key(announcement.cache_key)
        metadata = announcement.to_metadata()
        try:
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            from app.services.ai_cache_index import get_announcement_cache_index

            get_announcement_cache_index(CACHE_DIR).register(metadata)
        except Exception:
            _log.warning("Failed to write AI host cache metadata", exc_info=True)

    @staticmethod
    def _bool_setting(settings: dict[str, Any] | None, key: str, default: bool) -> bool:
        if not isinstance(settings, dict):
            return bool(default)
        token = str(settings.get(key, str(default).lower()) or "").strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    @staticmethod
    def _int_setting(
        settings: dict[str, Any] | None,
        key: str,
        default: int,
        *,
        minimum: int = 1,
        maximum: int = 180,
    ) -> int:
        if not isinstance(settings, dict):
            return int(default)
        try:
            value = int(float(settings.get(key, default)))
        except (TypeError, ValueError):
            value = int(default)
        return max(minimum, min(maximum, value))

    def _effective_max_seconds(self, settings: dict[str, Any] | None, default: int = 15) -> int:
        return self._int_setting(
            settings,
            "ai_announcement_max_seconds",
            default,
            minimum=5,
            maximum=90,
        )

    @staticmethod
    def _voice_persona(settings: dict[str, Any] | None) -> str:
        token = str((settings or {}).get("ai_voice_persona", "auto") or "auto").strip().lower()
        if token in {
            "warm_radio_host",
            "energetic_morning",
            "smooth_evening",
            "professional_announcer",
            "classical_host",
            "casual_friend",
            "jazz_lounge",
            "news_bulletin",
        }:
            return token
        if token in {"morning", "afternoon", "evening", "night"}:
            return token
        return get_current_persona()

    @staticmethod
    def _prompt_template(settings: dict[str, Any] | None) -> str:
        token = str((settings or {}).get("ai_prompt_template", "") or "").strip()
        return token or DEFAULT_PROMPT_TEMPLATE

    def _history_line(self, *, enabled: bool, artist: str) -> str:
        if not enabled:
            return ""
        try:
            history = self._history()
            anniversary = history.get_composer_anniversary(artist) if artist else None
            if anniversary:
                years = int(anniversary.get("anniversary_years", 0) or 0)
                ann_type = str(anniversary.get("anniversary_type", "anniversary") or "anniversary")
                composer = str(anniversary.get("composer_name", artist) or artist)
                if years > 0:
                    return f" Today marks the {years} year {ann_type} anniversary of {composer}."
                return f" Today is a notable {ann_type} anniversary for {composer}."
            event = history.get_today_event()
            if event:
                title = str(event.get("title", "") or "").strip()
                if title:
                    return f" Today in music history: {title}."
        except Exception:
            _log.warning("AI host history lookup failed", exc_info=True)
        return ""

    @staticmethod
    def _educational_line(enabled: bool) -> str:
        if not enabled:
            return ""
        return " Listen for the phrasing, balance, and dynamic contrast in the performance."

    @staticmethod
    def _listener_friendly_title(title: str) -> str:
        text = str(title or "").strip()
        if not text:
            return ""
        text = re.sub(
            r"\s*[-|]\s*(RadioTEDU|remaster(ed)?|official audio).*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:K|KV|BWV|D|RV|HWV|Hob|Op|No)\.?\s*\d+[a-z]?(?:[/-]\d+)?\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:catalogue|catalog|opus|movement|mov\.?|mvt\.?)\s+\w+\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b(?:in\s+)?[A-G](?:\s*(?:flat|sharp|major|minor|maj\.?|min\.?)){1,3}\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*[:,;]\s*$", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" -,:;")
        return text or str(title or "").strip()

    @staticmethod
    def _render_template(template: str, context: dict[str, Any]) -> str:
        try:
            rendered = str(template or DEFAULT_PROMPT_TEMPLATE).format_map(_SafeTemplateDict(context))
        except Exception:
            rendered = DEFAULT_PROMPT_TEMPLATE.format_map(_SafeTemplateDict(context))
        return rendered

    @staticmethod
    def _duration_seconds(audio_path: str, text: str) -> float:
        duration = float(probe_duration(audio_path) or 0.0)
        if duration > 0.0:
            return duration
        words = len(str(text or "").split())
        return max(2.0, round(words / _SPEECH_WORDS_PER_SECOND, 2))

    @staticmethod
    def _clean_text(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        value = re.sub(r"\*[^*]+\*", "", value)
        value = re.sub(r"#+", "", value)
        value = value.replace('"', "").replace("'", "")
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @staticmethod
    def _truncate_for_speech(text: str, max_seconds: int) -> str:
        words = str(text or "").split()
        max_words = max(8, int(max_seconds * _SPEECH_WORDS_PER_SECOND))
        if len(words) <= max_words:
            return AIHostService._ensure_complete_sentence(str(text or "").strip())
        clipped = " ".join(words[:max_words]).strip()
        complete = AIHostService._last_complete_sentence(clipped)
        if complete:
            return complete
        return AIHostService._ensure_complete_sentence(clipped)

    @staticmethod
    def _last_complete_sentence(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        matches = list(re.finditer(r"[.!?](?=\s|$)", value))
        if not matches:
            return ""
        end = matches[-1].end()
        if end < 16:
            return ""
        return value[:end].strip()

    @staticmethod
    def _ensure_complete_sentence(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        if value[-1] in ".!?":
            return value
        complete = AIHostService._last_complete_sentence(value)
        if complete:
            return complete
        return value.rstrip(",;:") + "."

    @staticmethod
    def _ensure_station_name(text: str, station_name: str) -> str:
        clean_text = str(text or "").strip()
        clean_station_name = str(station_name or "").strip()
        if not clean_station_name:
            return clean_text
        pattern = re.compile(rf"(?<!\\w){re.escape(clean_station_name)}(?!\\w)", re.IGNORECASE)
        if pattern.search(clean_text):
            return clean_text
        if not clean_text:
            return f"You're listening to {clean_station_name}."
        return f"You're listening to {clean_station_name}. {clean_text}"

    @staticmethod
    def _llm_max_tokens(max_seconds: int) -> int:
        return max(48, min(240, int(max_seconds * 5)))

    def _llm_messages(
        self,
        *,
        spoken_template: str,
        station_name: str,
        title: str,
        artist: str,
        spoken_title: str,
        persona: str,
        max_seconds: int,
        greeting: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are a warm, engaging radio host. You write short, natural spoken intros "
                    "for music tracks. Output one or two spoken sentences only. Finish every sentence. "
                    "Vary your opening EVERY time so it never sounds templated. No markdown. No quotes. "
                    "No stage directions."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Station: {station_name}\n"
                    f"Raw track title, for context only: {title}\n"
                    f"Listener-friendly title hint: {spoken_title}\n"
                    f"Artist or composer: {artist}\n"
                    f"Time of day: {greeting}.\n"
                    f"Length limit: under {max_seconds} spoken seconds (roughly up to 35 words).\n"
                    "Write one or two short, natural sentences for airplay. "
                    "Vary the opening each time: sometimes a fitting time-of-day greeting (use the time of "
                    "day above, e.g. 'Good evening'), sometimes a brief mood or listening observation, and "
                    "sometimes go straight into the song. Do NOT invent the day of the week or a specific "
                    "clock time. "
                    "Mention the artist or composer and only the listener-friendly title hint. "
                    "Do not read the raw title verbatim. Omit catalog numbers, opus numbers, keys, "
                    "movement numbers, file metadata, and long subtitles. "
                    "Do not say 'Up next is' or 'Upnext is'. "
                    "Keep it speech-friendly. Expand abbreviations for speech. "
                    "End with a period, question mark, or exclamation mark.\n"
                    f"Prompt guidance, do not copy as a template: {spoken_template}"
                ),
            },
        ]

    def _llm_prompt(
        self,
        *,
        spoken_template: str,
        station_name: str,
        title: str,
        artist: str,
        spoken_title: str,
        persona: str,
        max_seconds: int,
        greeting: str,
    ) -> str:
        return (
            f"You are a warm, engaging professional radio host for {station_name}.\n"
            "Write one short spoken script for the next announcement.\n"
            f"Constraints: under {max_seconds} seconds when spoken (roughly up to 35 words), "
            "one or two short natural sentences, conversational radio host style, "
            "varied wording, no markdown, no quotes, no stage directions.\n"
            f"Time of day: {greeting}. Vary the opening EVERY time: sometimes a fitting time-of-day "
            "greeting (e.g. 'Good evening'), sometimes a brief mood or observation, sometimes straight "
            "into the song. Do NOT invent the day of the week or a specific clock time.\n"
            "IMPORTANT: Write speech-friendly text. Spell out abbreviations (Pt.→Part, DJ→D J), "
            "use proper punctuation for natural pauses (commas, periods). Avoid semicolons, "
            "dashes, or special characters. Write exactly as you would speak it on air.\n"
            f"Persona: {persona}\n"
            "Do not say 'Up next is' or 'Upnext is'. Do not recite the raw full title. "
            "Use the artist or composer plus the listener-friendly title only. "
            "Skip catalog numbers, opus numbers, keys, movements, and file metadata.\n"
            f"Raw track title, for context only: {title}\n"
            f"Listener-friendly title hint: {spoken_title}\n"
            f"Artist or composer: {artist}\n"
            f"Prompt guidance, do not copy as a template: {spoken_template}\n"
            "Return only the spoken script."
        )

    def _generate_spoken_text(
        self,
        *,
        station_name: str,
        title: str,
        artist: str,
        settings: dict[str, Any] | None,
    ) -> tuple[str, str]:
        persona = self._voice_persona(settings)
        greeting = get_time_greeting()
        max_seconds = self._effective_max_seconds(settings, 15)
        include_history = self._bool_setting(settings, "ai_include_music_history", True)
        educational_enabled = self._bool_setting(settings, "ai_educational_segments", False)
        history_line = self._history_line(enabled=include_history, artist=artist)
        educational_line = self._educational_line(educational_enabled)
        spoken_title = self._listener_friendly_title(title) or "the piece"
        speech_title = re.sub(
            r"\s*[-|]\s*(RadioTEDU|remaster(ed)?|official audio).*$",
            "",
            str(title or "").strip(),
            flags=re.IGNORECASE,
        ).strip() or "the next piece"
        context = {
            "station_name": station_name,
            "track_title": speech_title,
            "spoken_track_title": spoken_title,
            "artist": artist,
            "artist_phrase": f" by {artist}" if artist else "",
            "history_line": history_line,
            "educational_line": educational_line,
            "persona": persona,
            "time_of_day": persona,
            "greeting": greeting,
        }
        rendered_template = self._render_template(self._prompt_template(settings), context)
        llm_model = str((settings or {}).get("ai_llm_model", DEFAULT_LLM_MODEL_ID) or DEFAULT_LLM_MODEL_ID)
        llm_text = self._generate_text(
            self._llm_messages(
                spoken_template=rendered_template,
                station_name=station_name,
                title=title,
                artist=artist,
                spoken_title=spoken_title,
                persona=persona,
                max_seconds=max_seconds,
                greeting=greeting,
            ),
            max_tokens=self._llm_max_tokens(max_seconds),
            model_token=llm_model,
        )
        if llm_text:
            cleaned = self._clean_text(llm_text)
            if re.search(
                r"(?:Raw track title|Listener-friendly title hint|Length limit|Prompt guidance)",
                cleaned,
                re.IGNORECASE,
            ):
                cleaned = ""
            if cleaned:
                return self._truncate_for_speech(
                    cleaned,
                    max_seconds,
                ), self._llm_provider_status(llm_model)
        spoken_fallback = self._render_template(DEFAULT_SPOKEN_FALLBACK_TEMPLATE, context)
        return self._truncate_for_speech(
            self._clean_text(spoken_fallback),
            max_seconds,
        ), "template-fallback"

    def _build_announcement(
        self,
        *,
        station_id: int,
        station_name: str,
        announcement_type: str,
        title: str,
        artist: str,
        spoken_text: str,
        settings: dict[str, Any] | None,
        cache_payload: dict[str, Any],
        llm_provider: str,
        dedupe_key: str = "",
    ) -> AIAnnouncement | None:
        requested_dedupe_key = str(dedupe_key or "").strip()
        cache_basis = dict(cache_payload or {})
        if requested_dedupe_key:
            # Queue-scoped dedupe keys guarantee a fresh intro per target item while
            # still allowing prefetch and worker pickup to share the same artifact.
            cache_basis["dedupe_key"] = requested_dedupe_key

        cache_key = self._cache_key_for_payload(cache_basis)
        cached = self._load_cached_announcement(cache_key)
        if cached is not None:
            if requested_dedupe_key and cached.dedupe_key != requested_dedupe_key:
                cached.dedupe_key = requested_dedupe_key
                self._store_cached_announcement(cached)
            return cached

        output_path = self._audio_path_for_key(cache_key)
        persona = self._voice_persona(settings)
        tts_provider = self._synthesize(
            spoken_text,
            output_path,
            persona=persona,
            tts_model_path=str((settings or {}).get("ai_tts_model_path", "") or ""),
            settings=settings,
        )
        if not tts_provider and self._configured_tts_provider(settings) == "local-qwen-tts":
            fallback_title = re.sub(
                r"^\s*AI\s+Intro\s*-\s*",
                "",
                str(title or "the next piece"),
                flags=re.IGNORECASE,
            ).strip()
            fallback_title = self._listener_friendly_title(fallback_title) or "the next piece"
            fallback_text = self._truncate_for_speech(
                self._clean_text(f"Now playing {fallback_title} on {station_name}."),
                8,
            )
            if fallback_text and fallback_text != spoken_text:
                tts_provider = self._synthesize(
                    fallback_text,
                    output_path,
                    persona=persona,
                    tts_model_path=str((settings or {}).get("ai_tts_model_path", "") or ""),
                    settings=settings,
                )
                if tts_provider:
                    spoken_text = fallback_text
        if not tts_provider:
            return None

        announcement = AIAnnouncement(
            cache_key=cache_key,
            station_id=int(station_id),
            station_name=str(station_name or "Main Radio"),
            announcement_type=str(announcement_type or "announcement"),
            title=str(title or "AI Announcement"),
            artist=str(artist or "AI Host"),
            text=spoken_text,
            audio_path=str(output_path),
            duration_seconds=self._duration_seconds(str(output_path), spoken_text),
            llm_provider=str(llm_provider or "template-fallback"),
            tts_provider=str(tts_provider),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            dedupe_key=str(dedupe_key or ""),
        )
        self._generated_count += 1
        self._store_cached_announcement(announcement)
        return announcement

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
        clean_title = str(title or "").strip()
        if not clean_title:
            return None
        spoken_text, llm_provider = self._generate_spoken_text(
            station_name=station_name,
            title=clean_title,
            artist=str(artist or "").strip(),
            settings=settings,
        )
        if len(spoken_text) < 8:
            return None
        cache_payload = {
            "station_id": int(station_id),
            "station_name": str(station_name or "Main Radio"),
            "announcement_type": "track_intro",
            "title": clean_title,
            "artist": str(artist or "").strip(),
            "prompt_template": self._prompt_template(settings),
            "voice_persona": self._voice_persona(settings),
            "time_greeting": get_time_greeting(),
            "max_seconds": self._effective_max_seconds(settings, 15),
            "include_history": self._bool_setting(settings, "ai_include_music_history", True),
            "educational_enabled": self._bool_setting(settings, "ai_educational_segments", False),
            "spoken_text": spoken_text,
            "tts_provider": self._configured_tts_provider(settings),
        }
        return self._build_announcement(
            station_id=station_id,
            station_name=station_name,
            announcement_type="track_intro",
            title=f"AI Intro - {clean_title}",
            artist="AI Host",
            spoken_text=spoken_text,
            settings=settings,
            cache_payload=cache_payload,
            llm_provider=llm_provider,
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
        clean_station_name = str(station_name or "").strip() or "Main Radio"
        persona = self._voice_persona(settings)
        max_seconds = self._effective_max_seconds(settings, 15)

        prompt = [
            {
                "role": "system",
                "content": (
                    "You write concise, speech-friendly station identification lines. "
                    "Output only the final spoken script in natural English."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Station: {clean_station_name}\n"
                    f"Length limit: under {max_seconds} spoken seconds.\n"
                    "Write one short station ID for airplay. One sentence only. No quotes. No markdown."
                ),
            },
        ]

        llm_model = str((settings or {}).get("ai_llm_model", DEFAULT_LLM_MODEL_ID) or DEFAULT_LLM_MODEL_ID)
        spoken_text = self._generate_text(
            prompt,
            max_tokens=self._llm_max_tokens(max_seconds),
            model_token=llm_model,
        )
        llm_provider = self._llm_provider_status(llm_model) if spoken_text else "template-fallback"

        if not spoken_text:
            spoken_text = self._truncate_for_speech(
                self._clean_text(f"This is {clean_station_name}, live on air."),
                max_seconds,
            )
        else:
            spoken_text = self._truncate_for_speech(
                self._clean_text(spoken_text),
                max_seconds,
            )

        cache_payload = {
            "station_id": int(station_id),
            "station_name": clean_station_name,
            "announcement_type": "station_id",
            "voice_persona": persona,
            "spoken_text": spoken_text,
            "tts_provider": self._configured_tts_provider(settings),
        }
        return self._build_announcement(
            station_id=station_id,
            station_name=clean_station_name,
            announcement_type="station_id",
            title="AI Station ID",
            artist="AI Host",
            spoken_text=spoken_text,
            settings=settings,
            cache_payload=cache_payload,
            llm_provider=llm_provider,
            dedupe_key=dedupe_key,
        )

    def generate_track_introduction(
        self,
        composer: str,
        title: str,
        artist: str = "",
        *,
        station_id: int = 1,
        station_name: str = "Main Radio",
        settings: dict[str, Any] | None = None,
    ) -> Optional[str]:
        """
        Backward-compatible helper that returns only the generated audio path.
        """
        announcement = self.generate_track_intro_announcement(
            station_id=station_id,
            station_name=station_name,
            title=title,
            artist=artist or composer,
            settings=settings,
        )
        if announcement is None:
            return None
        return announcement.audio_path

    @staticmethod
    def _cache_clear_cutoff() -> float:
        try:
            return float(
                CACHE_CLEAR_MARKER.read_text(encoding="utf-8").strip() or "0"
            )
        except Exception:
            return 0.0

    @classmethod
    def _cache_path_visible(
        cls, path: str | Path, cutoff: float | None = None
    ) -> bool:
        clear_cutoff = (
            cls._cache_clear_cutoff() if cutoff is None else float(cutoff)
        )
        if clear_cutoff <= 0:
            return True
        try:
            return Path(path).stat().st_mtime > clear_cutoff
        except OSError:
            return False

    @staticmethod
    def _latest_cache_file_mtime() -> float:
        latest = 0.0
        try:
            for path in CACHE_DIR.glob("announcement_*"):
                try:
                    latest = max(latest, path.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return 0.0
        return latest

    def count_cached_announcements(
        self,
        *,
        station_id: int | None = None,
        scan_limit: int | None = None,
    ) -> int:
        count = 0
        cutoff = self._cache_clear_cutoff()
        scanned = 0
        max_scan = None if scan_limit is None else max(1, int(scan_limit))
        for metadata_path in CACHE_DIR.glob("announcement_*.json"):
            scanned += 1
            if max_scan is not None and scanned > max_scan:
                break
            if not self._cache_path_visible(metadata_path, cutoff):
                continue
            if station_id is None:
                count += 1
                continue
            try:
                payload = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
                if int(payload.get("station_id", 1) or 1) == int(station_id):
                    count += 1
            except Exception:
                continue
        return count

    def list_cached_announcements(
        self,
        *,
        station_id: int | None = None,
        limit: int | None = None,
        include_duration_fallback: bool = True,
        verify_audio_files: bool = True,
        scan_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        cutoff = self._cache_clear_cutoff()
        max_items = None if limit is None else max(0, int(limit))
        max_scan = None if scan_limit is None else max(1, int(scan_limit))
        scanned = 0
        for metadata_path in sorted(
            CACHE_DIR.glob("announcement_*.json"), reverse=True
        ):
            scanned += 1
            if max_scan is not None and scanned > max_scan:
                break
            if not self._cache_path_visible(metadata_path, cutoff):
                continue
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            audio_path = Path(str(payload.get("audio_path", "") or ""))
            if verify_audio_files and not audio_path.exists():
                continue
            payload_station_id = int(payload.get("station_id", 1) or 1)
            if station_id is not None and payload_station_id != int(station_id):
                continue
            duration = float(payload.get("duration_seconds", 0.0) or 0.0)
            if duration <= 0.0 and include_duration_fallback:
                duration = self._duration_seconds(str(audio_path), str(payload.get("text", "")))
            output.append(
                {
                    "cache_key": str(payload.get("cache_key", "") or audio_path.stem),
                    "station_id": payload_station_id,
                    "station_name": str(payload.get("station_name", "") or ""),
                    "announcement_type": str(payload.get("announcement_type", "announcement") or "announcement"),
                    "title": str(payload.get("title", "AI Announcement") or "AI Announcement"),
                    "artist": str(payload.get("artist", "AI Host") or "AI Host"),
                    "path": str(audio_path),
                    "size_bytes": (
                        int(audio_path.stat().st_size)
                        if verify_audio_files
                        else int(payload.get("size_bytes", 0) or 0)
                    ),
                    "duration_seconds": duration,
                    "llm_provider": str(payload.get("llm_provider", "template-fallback") or "template-fallback"),
                    "tts_provider": str(payload.get("tts_provider", "unavailable") or "unavailable"),
                    "generated_at": str(payload.get("generated_at", "") or ""),
                    "text": str(payload.get("text", "") or ""),
                }
            )
            if max_items is not None and len(output) >= max_items:
                break
        return output

    def is_ready(self, *, settings: dict[str, Any] | None = None) -> bool:
        provider = self._configured_tts_provider(settings)
        if provider == "windows-sapi":
            return self._windows_sapi_available()
        if provider == "edge-tts":
            return self._edge_tts_available()
        if provider == "omnivoice":
            return self._omnivoice_available()
        tts_model_path = str((settings or {}).get("ai_tts_model_path", "") or "")
        return bool(self._tts_loaded or self._resolve_tts_dir(tts_model_path))

    def generate_batch_track_intros(
        self,
        tracks: list[dict[str, str]],
        *,
        station_id: int = 1,
        station_name: str = "Main Radio",
        settings: dict[str, Any] | None = None,
    ) -> list[AIAnnouncement]:
        """
        Generate track intro announcements for multiple tracks.
        Returns list of generated announcements (excludes failed ones).
        """
        announcements = []
        for track in tracks:
            title = str(track.get("title", "")).strip()
            artist = str(track.get("artist", "")).strip()
            if not title:
                continue
            announcement = self.generate_track_intro_announcement(
                station_id=station_id,
                station_name=station_name,
                title=title,
                artist=artist,
                settings=settings,
            )
            if announcement:
                announcements.append(announcement)
        return announcements

    def get_status(
        self,
        *,
        settings: dict[str, Any] | None = None,
        station_id: int | None = None,
        cache_scan_limit: int | None = None,
    ) -> dict[str, Any]:
        llm_model = str((settings or {}).get("ai_llm_model", DEFAULT_LLM_MODEL_ID) or DEFAULT_LLM_MODEL_ID)
        tts_model_path = str((settings or {}).get("ai_tts_model_path", "") or "")
        cache_size = self.count_cached_announcements(
            station_id=station_id, scan_limit=cache_scan_limit
        )
        provider = self._configured_tts_provider(settings)
        ollama_model = self._ollama_model_name(llm_model)
        llm_model_dir = self._resolve_llm_dir(llm_model)
        llm_model_exists = self._ollama_available(ollama_model) if ollama_model else bool(llm_model_dir)
        llm_model_dir_label = f"ollama:{ollama_model}" if ollama_model else str(llm_model_dir or "")
        if provider == "windows-sapi":
            tts_model_exists = self._windows_sapi_available()
            tts_provider = "windows-sapi" if tts_model_exists else "windows-sapi-unavailable"
            ready = bool(tts_model_exists)
            tts_loaded = bool(tts_model_exists)
            tts_model_dir = ""
        elif provider == "omnivoice":
            tts_model_exists = self._omnivoice_available()
            tts_provider = "omnivoice" if tts_model_exists else "omnivoice-unavailable"
            ready = bool(tts_model_exists)
            tts_loaded = bool(tts_model_exists)
            tts_model_dir = ""
        elif provider == "edge-tts":
            tts_model_exists = self._edge_tts_available()
            tts_provider = "edge-tts" if tts_model_exists else "edge-tts-unavailable"
            ready = bool(tts_model_exists)
            tts_loaded = bool(tts_model_exists)
            tts_model_dir = ""
        else:
            tts_model_exists = bool(self._resolve_tts_dir(tts_model_path))
            tts_provider = self._tts_provider_status(tts_model_path)
            ready = self.is_ready(settings=settings)
            tts_loaded = bool(self._tts_loaded)
            tts_model_dir = str(self._resolve_tts_dir(tts_model_path) or "")
        return {
            "llm_loaded": self._llm_loaded,
            "llm_model_exists": bool(llm_model_exists),
            "llm_model_dir": llm_model_dir_label,
            "llm_provider": self._llm_provider_status(llm_model),
            "tts_loaded": tts_loaded,
            "tts_model_exists": bool(tts_model_exists),
            "tts_model_dir": tts_model_dir,
            "tts_provider": tts_provider,
            "current_persona": self._voice_persona(settings),
            "cache_size": cache_size,
            "announcements_generated": cache_size,
            "prompt_template_configured": bool(str((settings or {}).get("ai_prompt_template", "") or "").strip()),
            "ready": ready,
        }

    def warmup(self, *, settings: dict[str, Any] | None = None) -> bool:
        _log.info("Warming up AI host providers")
        llm_model = str((settings or {}).get("ai_llm_model", DEFAULT_LLM_MODEL_ID) or DEFAULT_LLM_MODEL_ID)
        tts_model_path = str((settings or {}).get("ai_tts_model_path", "") or "")
        provider = self._configured_tts_provider(settings)
        llm_ok = self._load_llm(llm_model)
        if provider == "windows-sapi":
            tts_ok = self._windows_sapi_available()
        elif provider == "edge-tts":
            tts_ok = self._edge_tts_available()
        elif provider == "omnivoice":
            tts_ok = self._omnivoice_available()
        else:
            # Local TTS runs in a subprocess to avoid loading the model in the API process.
            tts_dir = self._resolve_tts_dir(tts_model_path)
            tts_ok = bool(tts_dir and tts_dir.exists() and self._qwen_tts_cli_path().exists())
            self._tts_model_dir = tts_dir
            if tts_ok:
                self._tts_loaded = True
        _log.info(
            "AI host warmup complete: llm=%s tts=%s",
            "ok" if llm_ok else self._llm_provider_status(llm_model),
            "ok" if tts_ok else self._tts_provider_status(tts_model_path),
        )
        return self.is_ready(settings=settings)

    @staticmethod
    def _delete_cache_files_before(cutoff: float) -> None:
        for path in CACHE_DIR.glob("announcement_*"):
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        gc.collect()

    def clear_cache(self) -> dict[str, Any]:
        cleared_at = max(time.time(), self._latest_cache_file_mtime() + 0.001)
        self._announcement_cache.clear()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CACHE_CLEAR_MARKER.write_text(
                str(cleared_at), encoding="utf-8"
            )
        except OSError:
            _log.exception("Failed to write AI cache clear marker")
        threading.Thread(
            target=self._delete_cache_files_before,
            args=(cleared_at,),
            name="ai-cache-clear",
            daemon=True,
        ).start()
        return {"cleared_at": cleared_at, "background_delete": True}


class _no_grad:
    """Simple no_grad context to avoid importing torch at module import time."""

    def __enter__(self):
        try:
            import torch

            self._ctx = torch.no_grad()
            self._ctx.__enter__()
        except Exception:
            self._ctx = None

    def __exit__(self, *args):
        if self._ctx:
            self._ctx.__exit__(*args)


_instance: Optional[AIHostService] = None


def get_ai_host() -> AIHostService:
    global _instance
    if _instance is None:
        _instance = AIHostService()
    return _instance


def reset_ai_host():
    global _instance
    _instance = None
