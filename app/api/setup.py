from __future__ import annotations

import hashlib
import json
import queue
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.health import describe_dependency
from app.audio.gst_pipeline import resolve_stream_profile
from app.auth.dependencies import require_any_permission
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.repositories.station_repo import StationRepository
from app.runtime_paths import resolve_binary

router = APIRouter()

SETUP_COMPLETED_KEY = "setup.onboarding_completed"
SETUP_LAST_VERIFIED_AT_KEY = "setup.last_successful_validation_time"
SETUP_CONFIGURED_AT_KEY = "setup.last_configured_at"
LOCAL_VERIFIED_KEY = "setup.local_output_verified"
LOCAL_SIGNATURE_KEY = "setup.local_output_signature"
STREAM_VERIFIED_KEY = "setup.stream_output_verified"
STREAM_SIGNATURE_KEY = "setup.stream_output_signature"
STREAM_TEST_PASSED_KEY = "setup.stream_output_test_passed"
STREAM_TEST_MESSAGE_KEY = "setup.stream_output_test_message"
STREAM_TESTED_AT_KEY = "setup.stream_output_tested_at"
AI_VERIFIED_KEY = "setup.ai_tts_verified"
AI_SIGNATURE_KEY = "setup.ai_tts_signature"
AI_TEST_PASSED_KEY = "setup.ai_tts_test_passed"
AI_TEST_MESSAGE_KEY = "setup.ai_tts_test_message"
AI_TESTED_AT_KEY = "setup.ai_tts_tested_at"
AI_STACK_KEY = "setup.ai_stack"
AI_WARMTH_KEY = "setup.ai_warmth"
STARTUP_AI_STATE_KEY = "startup_ai_readiness_state"
STARTUP_AI_READY_INTROS_KEY = "startup_ai_ready_intro_count"
STARTUP_AI_REQUIRED_INTROS_KEY = "startup_ai_required_intro_count"
DEPLOYMENT_CERTIFIED_KEY = "setup.deployment_certified"
DEPLOYMENT_CERTIFIED_AT_KEY = "setup.deployment_certified_at"
DEPLOYMENT_CERTIFICATE_ID_KEY = "setup.deployment_certificate_id"
DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY = "setup.deployment_certificate_signature"

DEFAULT_AI_STACK = "ollama_local_qwen_tts"
DEFAULT_AI_LLM_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_AI_TTS_PROVIDER = "local-qwen-tts"
DEFAULT_STREAM_CODEC_PROFILE = "he_aac_192"
DEFAULT_STREAM_BITRATE_KBPS = 192
DEFAULT_STATION_NAME = "RadioTEDU OnAir"
DEFAULT_STREAM_NAME = "RadioTEDU OnAir"
AI_TEST_TIMEOUT_SECONDS = 180

VERIFIABLE_CHECKS = {"local_output", "stream_output", "ai_tts"}
WARMTH_PRESET_MAP = {
    "warm": "warm_radio_host",
    "energetic": "energetic_morning",
    "smooth": "smooth_evening",
    "professional": "professional_announcer",
}
PERSONA_TO_WARMTH = {value: key for key, value in WARMTH_PRESET_MAP.items()}
CODEC_PRESETS = [
    {
        "id": "he_aac_192",
        "label": "HE-AAC v1 Normal — 192 kbps",
        "codec": "aac",
        "bitrate_kbps": 192,
    },
    {
        "id": "he_aac_96",
        "label": "HE-AAC v1 Low — 96 kbps",
        "codec": "aac",
        "bitrate_kbps": 96,
    },
]


class SetupVerificationPayload(BaseModel):
    station_id: int
    check: str


class SetupStationPayload(BaseModel):
    station_id: int


class SetupConfigurationPayload(BaseModel):
    station_id: int
    station_name: str = DEFAULT_STATION_NAME
    local_output_enabled: bool = True
    output_device_id: str = ""
    icecast_enabled: bool = True
    icecast_url: str = ""
    icecast_host: str = "127.0.0.1"
    icecast_port: int = 8000
    icecast_mount: str = "/live"
    icecast_user: str = "source"
    icecast_password: str = ""
    stream_codec_profile: str = DEFAULT_STREAM_CODEC_PROFILE
    ai_enabled: bool = True
    ai_warmth: str = "warm"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_port(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return int(default)


def _stable_signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check(name: str, label: str, ready: bool, message: str, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "ready": bool(ready),
        "status": "ready" if ready else "action_required",
        "message": message,
        "details": details,
    }


def _normalize_stream_profile(raw_profile: Any, raw_bitrate: Any = DEFAULT_STREAM_BITRATE_KBPS) -> tuple[str, int]:
    token = str(raw_profile or "").strip().lower().replace("-", "_").replace("+", "_plus_")
    approved = {"he_aac_96": 96, "he_aac_192": 192, "opus_192": 192}
    if token in approved:
        return token, approved[token]
    try:
        bitrate = max(32, min(512, int(raw_bitrate)))
    except (TypeError, ValueError):
        bitrate = DEFAULT_STREAM_BITRATE_KBPS
    del bitrate
    return DEFAULT_STREAM_CODEC_PROFILE, DEFAULT_STREAM_BITRATE_KBPS


def _parse_icecast_url(raw: Any) -> dict[str, Any]:
    value = str(raw or "").strip()
    if not value:
        return {}
    parse_value = value if "://" in value else f"http://{value}"
    parsed = urlparse(parse_value)
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="Icecast URL must include a server host")
    try:
        port = int(parsed.port or 8000)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Icecast URL contains an invalid port") from exc
    mount = unquote(str(parsed.path or "").strip())
    return {
        "host": parsed.hostname.strip(),
        "port": max(1, min(65535, port)),
        "mount": _normalize_mount(mount) if mount and mount != "/" else "",
        "user": unquote(parsed.username or "").strip(),
        "password": unquote(parsed.password or ""),
    }


def _compose_icecast_url(host: Any, port: Any, mount: Any) -> str:
    normalized_host = str(host or "").strip()
    normalized_port = _parse_port(port, 8000)
    normalized_mount = _normalize_mount(mount)
    if not normalized_host:
        return ""
    return f"http://{normalized_host}:{normalized_port}{normalized_mount}"


def _normalize_mount(raw: Any) -> str:
    token = str(raw or "").strip() or "/live"
    return token if token.startswith("/") else f"/{token}"


def _warmth_to_persona(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in WARMTH_PRESET_MAP:
        return WARMTH_PRESET_MAP[token]
    if token in PERSONA_TO_WARMTH:
        return token
    return WARMTH_PRESET_MAP["warm"]


def _persona_to_warmth(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in PERSONA_TO_WARMTH:
        return PERSONA_TO_WARMTH[token]
    if token in WARMTH_PRESET_MAP:
        return token
    return "warm"


def _station_row(conn, station_id: int):
    repo = StationRepository(conn)
    rows = list(repo.list_all())
    if not rows:
        created_id = repo.create(DEFAULT_STATION_NAME)
        repo.set_active(created_id)
        rows = list(repo.list_all())
    sid = int(station_id or 0)
    station = next((row for row in rows if int(row["id"]) == sid), None)
    if station is None:
        active = repo.get_active()
        station = active or rows[0]
    return station, rows


def _settings(conn, station_id: int) -> dict[str, str]:
    return SettingsRepository(conn).get_station(int(station_id))


def _output_payload(conn, station_id: int, station_settings: dict[str, str]) -> dict[str, Any]:
    row = StationOutputRepository(conn).get(int(station_id))
    if row is not None:
        profile, bitrate = _normalize_stream_profile(
            row["stream_codec_profile"],
            row["stream_bitrate_kbps"],
        )
        return {
            "local_output_enabled": bool(row["local_output_enabled"]),
            "output_device_id": str(row["output_device_id"] or "").strip(),
            "icecast_enabled": bool(row["icecast_enabled"]),
            "icecast_host": str(row["icecast_host"] or "").strip(),
            "icecast_port": _parse_port(row["icecast_port"], 0),
            "icecast_mount": str(row["icecast_mount"] or "").strip(),
            "icecast_user": str(row["icecast_user"] or "").strip(),
            "icecast_password": str(row["icecast_password"] or ""),
            "stream_codec_profile": profile,
            "stream_bitrate_kbps": bitrate,
            "icecast_tls_enabled": _truthy(
                station_settings.get(
                    "icecast_tls_enabled",
                    str(int(row["icecast_port"] or 0) == 443).lower(),
                )
            ),
        }

    output_mode = str(station_settings.get("output_mode", "speaker") or "speaker").strip().lower()
    speaker_monitor = _truthy(station_settings.get("speaker_monitor_enabled", "true"))
    profile, bitrate = _normalize_stream_profile(
        station_settings.get("stream_codec_profile", DEFAULT_STREAM_CODEC_PROFILE),
        station_settings.get("stream_bitrate_kbps", DEFAULT_STREAM_BITRATE_KBPS),
    )
    return {
        "local_output_enabled": output_mode != "icecast" or speaker_monitor,
        "output_device_id": "",
        "icecast_enabled": output_mode == "icecast",
        "icecast_host": str(station_settings.get("icecast_host", "127.0.0.1") or "127.0.0.1").strip(),
        "icecast_port": _parse_port(station_settings.get("icecast_port", "8000") or "8000", 0),
        "icecast_mount": str(station_settings.get("icecast_mount", "/live") or "/live").strip(),
        "icecast_user": str(
            station_settings.get("icecast_username", station_settings.get("icecast_user", "source")) or "source"
        ).strip(),
        "icecast_password": str(station_settings.get("icecast_password", "") or ""),
        "stream_codec_profile": profile,
        "stream_bitrate_kbps": bitrate,
        "icecast_tls_enabled": _truthy(
            station_settings.get(
                "icecast_tls_enabled",
                str(
                    _parse_port(
                        station_settings.get("icecast_port", "8000") or "8000",
                        0,
                    )
                    == 443
                ).lower(),
            )
        ),
    }


def _dependency_state() -> dict[str, dict[str, Any]]:
    from app.dependency_bootstrap import refresh_dependency_state

    state = refresh_dependency_state()
    return {
        "webview2": dict(state.get("webview2-runtime") or {}),
        "ollama": dict(state.get("ollama.exe") or {}),
        "python_runtime": dict(state.get("python-runtime") or {}),
        "qwen_tts_runtime": dict(state.get("qwen-tts-runtime") or {}),
    }


def _managed_qwen_tts_path() -> str:
    try:
        from app.dependency_bootstrap import managed_runtime_dir

        return str(managed_runtime_dir("qwen-tts"))
    except Exception:
        return ""


def _ollama_running() -> bool:
    return _tcp_reachable("127.0.0.1", 11434)


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=1.0):
            return True
    except Exception:
        return False


def _icecast_output_url(output: dict[str, Any]) -> str:
    mount = _normalize_mount(output.get("icecast_mount", "/live"))
    user = quote(str(output.get("icecast_user") or ""), safe="")
    password = quote(str(output.get("icecast_password") or ""), safe="")
    host = str(output.get("icecast_host") or "").strip()
    port = _safe_int(output.get("icecast_port", 8000), 8000)
    return f"icecast://{user}:{password}@{host}:{port}{mount}"


def _scrub_secret(value: Any, secret: Any) -> str:
    text = str(value or "")
    token = str(secret or "")
    if token:
        text = text.replace(token, "***")
    return text


def _icecast_test_protocol_args(output: dict[str, Any]) -> list[str]:
    """Match the production Icecast transport flags during connection tests."""
    args: list[str] = []
    if _truthy(output.get("icecast_tls_enabled", False)):
        args.extend(["-tls", "1"])
    if _truthy(output.get("icecast_legacy_source_enabled", False)):
        args.extend(["-legacy_icecast", "1"])
    return args


def _run_stream_output_test(output: dict[str, Any]) -> dict[str, Any]:
    """Publish two seconds of silence to prove credentials, mount, and codec."""
    if not bool(output.get("icecast_enabled")):
        return {"ok": True, "message": "Icecast stream output is disabled."}
    ffmpeg_bin = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
    if not ffmpeg_bin:
        return {"ok": False, "message": "FFmpeg is unavailable, so the Icecast test stream cannot run."}
    profile = resolve_stream_profile(
        str(output.get("stream_codec_profile") or DEFAULT_STREAM_CODEC_PROFILE),
        _safe_int(output.get("stream_bitrate_kbps", DEFAULT_STREAM_BITRATE_KBPS), DEFAULT_STREAM_BITRATE_KBPS),
    )
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
        "-f", "lavfi", "-t", "2.0", "-i", "anullsrc=r=48000:cl=stereo",
        "-vn", "-c:a", str(profile["ffmpeg_codec"]), "-b:a", f"{int(profile['bitrate_kbps'])}k",
    ]
    ffmpeg_profile = str(profile.get("ffmpeg_profile") or "").strip()
    if ffmpeg_profile:
        cmd.extend(["-profile:a", ffmpeg_profile])
    cmd.extend([
        "-content_type", str(profile["content_type"]), "-f", str(profile["format"]),
        *_icecast_test_protocol_args(output),
        "-metadata", "title=Radio output verification", _icecast_output_url(output),
    ])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Icecast accepted a connection too slowly; check the server URL, firewall, and mount."}
    except Exception as exc:
        return {"ok": False, "message": f"Icecast test stream could not start: {exc}"}
    if proc.returncode == 0:
        return {
            "ok": True,
            "message": (
                f"Icecast accepted the {profile['profile']} test stream on "
                f"{_compose_icecast_url(output.get('icecast_host'), output.get('icecast_port'), output.get('icecast_mount'))}."
            ),
        }
    stderr = _scrub_secret(proc.stderr, output.get("icecast_password"))[-700:].strip()
    if not stderr:
        stderr = f"ffmpeg exited with code {proc.returncode}"
    return {"ok": False, "message": f"Icecast test stream failed: {stderr}"}


def _live_stream_output_verification(station_id: int) -> dict[str, Any] | None:
    """Treat the active healthy encoder as the safest live destination test."""
    try:
        from app.api.runtime import runtime_registry

        status = dict(runtime_registry.status(int(station_id)) or {})
        branches = dict(status.get("branch_health") or {})
        if (
            bool(status.get("running"))
            and bool(status.get("output_feed_active", True))
            and bool(branches.get("icecast"))
        ):
            return {
                "ok": True,
                "message": (
                    "Verified by the active encoder: Icecast is connected and "
                    "receiving this station without interrupting the live mount."
                ),
            }
    except Exception:
        return None
    return None


def _ai_status(station_settings: dict[str, str]) -> dict[str, Any]:
    enabled = _truthy(station_settings.get("ai_host_enabled", "false"))
    deps = _dependency_state()
    if not enabled:
        return {
            "enabled": False,
            "ready": False,
            "reason": "AI host is disabled",
            "ollama_installed": bool(deps["ollama"].get("installed")),
            "ollama_running": _ollama_running(),
            "local_tts_runtime_installed": bool(deps["qwen_tts_runtime"].get("installed")),
        }
    try:
        from app.services.ai_host_fast import get_ai_host_fast

        status = dict(get_ai_host_fast().get_status(settings=station_settings))
    except Exception:
        try:
            from app.services.ai_host import get_ai_host

            status = dict(get_ai_host().get_status(settings=station_settings))
        except Exception as exc:
            return {
                "enabled": True,
                "ready": False,
                "reason": str(exc),
                "ollama_installed": bool(deps["ollama"].get("installed")),
                "ollama_running": _ollama_running(),
                "local_tts_runtime_installed": bool(deps["qwen_tts_runtime"].get("installed")),
            }
    status["enabled"] = True
    status["ollama_installed"] = bool(deps["ollama"].get("installed"))
    status["ollama_running"] = _ollama_running()
    status["local_tts_runtime_installed"] = bool(deps["qwen_tts_runtime"].get("installed"))
    return status


def _startup_ai_readiness(station_id: int, station_settings: dict[str, str]) -> dict[str, Any]:
    if not _truthy(station_settings.get("ai_host_enabled", "false")):
        return {
            "state": "disabled",
            "required_ready_track_intros": 0,
            "ready_track_intros": 0,
            "ready": True,
            "message": "AI voice is disabled. Continuity-only startup is allowed.",
            "prefetch": {},
        }

    try:
        from app.services.ai_prefetch import get_ai_prefetch, startup_buffer_target

        required_intros = startup_buffer_target(station_settings)
        readiness = dict(
            get_ai_prefetch().readiness_snapshot(
                int(station_id),
                lookahead=required_intros,
            )
        )
    except Exception as exc:
        readiness = {"error": str(exc)}
        required_intros = 4

    ready_track_intros = _safe_int(readiness.get("ready_track_intros", 0), 0)
    ready = ready_track_intros >= required_intros
    state = "ready" if ready else "warming"
    message = (
        f"AI startup buffer is primed with {ready_track_intros} ready intro(s)."
        if ready
        else f"Warm the AI voice and pre-generate {required_intros} intro(s) before streaming."
    )
    return {
        "state": state,
        "required_ready_track_intros": required_intros,
        "ready_track_intros": ready_track_intros,
        "ready": ready,
        "message": message,
        "prefetch": readiness,
    }


def _stored_verified(settings: dict[str, str], flag_key: str, signature_key: str, signature: str) -> bool:
    return _truthy(settings.get(flag_key, "false")) and str(settings.get(signature_key, "")) == signature


def _setup_config_snapshot(
    station: Any,
    station_settings: dict[str, str],
    output: dict[str, Any],
    startup_ai: dict[str, Any],
    dependencies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ai_warmth = _persona_to_warmth(station_settings.get("ai_voice_persona", station_settings.get(AI_WARMTH_KEY, "warm")))
    return {
        "station_id": int(station["id"]),
        "station_name": str(station["name"] or DEFAULT_STATION_NAME),
        "local_output_enabled": bool(output["local_output_enabled"]),
        "output_device_id": output["output_device_id"],
        "icecast_enabled": bool(output["icecast_enabled"]),
        "icecast_url": _compose_icecast_url(output["icecast_host"], output["icecast_port"], output["icecast_mount"]),
        "icecast_host": output["icecast_host"],
        "icecast_port": int(output["icecast_port"]),
        "icecast_mount": output["icecast_mount"],
        "icecast_user": output["icecast_user"],
        # Passwords are write-only at every API boundary. The decrypted value
        # remains available in ``output`` for readiness signatures and stream
        # tests, but must never be serialized back to the operator.
        "icecast_password": "",
        "icecast_password_configured": bool(str(output["icecast_password"] or "").strip()),
        "stream_codec_profile": output["stream_codec_profile"],
        "stream_bitrate_kbps": int(output["stream_bitrate_kbps"]),
        "ai_enabled": _truthy(station_settings.get("ai_host_enabled", "true")),
        "ai_stack": str(station_settings.get(AI_STACK_KEY, DEFAULT_AI_STACK) or DEFAULT_AI_STACK),
        "ai_warmth": ai_warmth,
        "ai_voice_preset": _warmth_to_persona(ai_warmth),
        "ai_tts_provider": str(station_settings.get("ai_tts_provider", DEFAULT_AI_TTS_PROVIDER) or DEFAULT_AI_TTS_PROVIDER),
        "ai_tts_model_path": str(station_settings.get("ai_tts_model_path", "") or ""),
        "ai_llm_model": str(station_settings.get("ai_llm_model", DEFAULT_AI_LLM_MODEL) or DEFAULT_AI_LLM_MODEL),
        "ai_test_passed": _truthy(station_settings.get(AI_TEST_PASSED_KEY, "false")),
        "ai_test_message": str(station_settings.get(AI_TEST_MESSAGE_KEY, "") or ""),
        "ai_tested_at": str(station_settings.get(AI_TESTED_AT_KEY, "") or ""),
        "startup_ai_readiness": startup_ai,
        "dependencies": dependencies,
    }


def _run_ai_voice_test(station_id: int, station_name: str, station_settings: dict[str, str]) -> dict[str, Any]:
    if not _truthy(station_settings.get("ai_host_enabled", "false")):
        return {"ok": True, "message": "AI voice is disabled. No TTS test is required."}

    try:
        try:
            from app.services.ai_host_fast import get_ai_host_fast

            ai = get_ai_host_fast()
            warmup_result = ai.warmup(settings=station_settings)
        except Exception:
            from app.services.ai_host import get_ai_host

            ai = get_ai_host()
            warmup_result = ai.warmup(settings=station_settings)

        announcement = ai.generate_station_id_announcement(
            station_id=int(station_id),
            station_name=station_name,
            settings=station_settings,
            dedupe_key=f"setup-ai-test:{int(station_id)}",
        )
        audio_path = Path(str(getattr(announcement, "audio_path", "") or ""))
        success = announcement is not None and audio_path.exists()
        message = (
            f"AI voice test succeeded using {station_settings.get('ai_tts_provider', DEFAULT_AI_TTS_PROVIDER)}."
            if success
            else "AI voice test could not synthesize a local announcement."
        )
        return {
            "ok": success,
            "message": message,
            "warmup_result": warmup_result,
            "audio_path": str(audio_path) if success else "",
        }
    except Exception as exc:
        return {"ok": False, "message": f"AI voice test failed: {exc}"}


def _run_ai_voice_test_guarded(station_id: int, station_name: str, station_settings: dict[str, str]) -> dict[str, Any]:
    result_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result = _run_ai_voice_test(station_id, station_name, station_settings)
        except Exception as exc:
            result = {"ok": False, "message": f"AI voice test failed: {exc}"}
        try:
            result_queue.put_nowait(result)
        except queue.Full:
            pass

    thread = threading.Thread(target=_worker, name="setup-ai-voice-test", daemon=True)
    thread.start()
    try:
        return result_queue.get(timeout=AI_TEST_TIMEOUT_SECONDS)
    except queue.Empty:
        return {
            "ok": False,
            "message": (
                f"AI voice test timed out after {AI_TEST_TIMEOUT_SECONDS} seconds. "
                "Use Repair Dependencies, then test the local voice again."
            ),
        }


def _required_checks(config: dict[str, Any]) -> set[str]:
    required = {"backend", "media_runtime", "webview2", "station"}
    if bool(config.get("local_output_enabled")):
        required.add("local_output")
    if bool(config.get("icecast_enabled")):
        required.add("stream_output")
    if bool(config.get("ai_enabled")):
        required.update({"ollama", "local_tts_runtime", "ai_tts", "startup_ai_buffer"})
    return required


def _deployment_signature(
    station_id: int,
    config: dict[str, Any],
    checks: list[dict[str, Any]],
    required: set[str],
) -> str:
    details_by_name = {str(check.get("name")): dict(check.get("details") or {}) for check in checks}
    startup = dict(config.get("startup_ai_readiness") or {})
    return _stable_signature(
        {
            "station_id": int(station_id),
            "station_name": str(config.get("station_name") or ""),
            "required_checks": sorted(required),
            "local_output_enabled": bool(config.get("local_output_enabled")),
            "output_device_id": str(config.get("output_device_id") or ""),
            "icecast_enabled": bool(config.get("icecast_enabled")),
            "icecast_host": str(config.get("icecast_host") or ""),
            "icecast_port": _safe_int(config.get("icecast_port", 0), 0),
            "icecast_mount": str(config.get("icecast_mount") or ""),
            "icecast_user": str(config.get("icecast_user") or ""),
            "stream_codec_profile": str(config.get("stream_codec_profile") or ""),
            "stream_bitrate_kbps": _safe_int(config.get("stream_bitrate_kbps", 0), 0),
            "local_output_signature": str(details_by_name.get("local_output", {}).get("signature") or ""),
            "stream_output_signature": str(details_by_name.get("stream_output", {}).get("signature") or ""),
            "ai_tts_signature": str(details_by_name.get("ai_tts", {}).get("signature") or ""),
            "ai_enabled": bool(config.get("ai_enabled")),
            "ai_stack": str(config.get("ai_stack") or ""),
            "ai_warmth": str(config.get("ai_warmth") or ""),
            "startup_ai_state": str(startup.get("state") or ""),
            "startup_ai_ready_track_intros": _safe_int(startup.get("ready_track_intros", 0), 0),
            "startup_ai_required_track_intros": _safe_int(startup.get("required_ready_track_intros", 0), 0),
        }
    )


def _build_state(request: Request | None, station_id: int) -> dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        station, stations = _station_row(conn, station_id)
        sid = int(station["id"])
        station_settings = _settings(conn, sid)
        output = _output_payload(conn, sid, station_settings)
        dependencies = _dependency_state()
        ai_status = _ai_status(station_settings)
        startup_ai = _startup_ai_readiness(sid, station_settings)
        config = _setup_config_snapshot(station, station_settings, output, startup_ai, dependencies)

        local_signature = _stable_signature(
            {
                "local_output_enabled": output["local_output_enabled"],
                "output_device_id": output["output_device_id"],
            }
        )
        stream_signature = _stable_signature(
            {
                "icecast_enabled": output["icecast_enabled"],
                "icecast_host": output["icecast_host"],
                "icecast_port": output["icecast_port"],
                "icecast_mount": output["icecast_mount"],
                "icecast_user": output["icecast_user"],
                "icecast_password": output["icecast_password"],
                "stream_codec_profile": output["stream_codec_profile"],
                "stream_bitrate_kbps": output["stream_bitrate_kbps"],
            }
        )
        ai_signature = _stable_signature(
            {
                "ai_host_enabled": station_settings.get("ai_host_enabled", "false"),
                "ai_llm_model": station_settings.get("ai_llm_model", ""),
                "ai_tts_provider": station_settings.get("ai_tts_provider", DEFAULT_AI_TTS_PROVIDER),
                "ai_tts_model_path": station_settings.get("ai_tts_model_path", ""),
                "ai_voice_persona": station_settings.get("ai_voice_persona", "warm_radio_host"),
                "setup_ai_stack": station_settings.get(AI_STACK_KEY, DEFAULT_AI_STACK),
            }
        )

        ffmpeg = describe_dependency("ffmpeg.exe", "ffmpeg")
        ffplay = describe_dependency("ffplay.exe", "ffplay")
        ffprobe = describe_dependency("ffprobe.exe", "ffprobe")
        media_ready = all(bool(item.get("found")) for item in (ffmpeg, ffplay, ffprobe))

        local_configured = bool(output["local_output_enabled"])
        local_verified = local_configured and _stored_verified(
            station_settings,
            LOCAL_VERIFIED_KEY,
            LOCAL_SIGNATURE_KEY,
            local_signature,
        )

        stream_configured = (
            bool(output["icecast_enabled"])
            and bool(output["icecast_host"])
            and 1 <= int(output["icecast_port"] or 0) <= 65535
            and bool(str(output["icecast_mount"] or "").strip())
            and bool(str(output["icecast_user"] or "").strip())
            and bool(str(output["icecast_password"] or "").strip())
        )
        stream_reachable = (
            _tcp_reachable(str(output["icecast_host"]), int(output["icecast_port"]))
            if stream_configured
            else False
        )
        setup_completed_flag = _truthy(station_settings.get(SETUP_COMPLETED_KEY, "false"))
        stream_test_passed = _truthy(station_settings.get(STREAM_TEST_PASSED_KEY, "false"))
        stream_test_message = str(station_settings.get(STREAM_TEST_MESSAGE_KEY, "") or "")
        stream_verified = stream_configured and stream_reachable and stream_test_passed and _stored_verified(
            station_settings,
            STREAM_VERIFIED_KEY,
            STREAM_SIGNATURE_KEY,
            stream_signature,
        )
        if stream_verified:
            stream_message = "Icecast source credentials, mount, and codec were verified with a test stream."
        elif stream_configured and not stream_reachable:
            stream_message = "Icecast destination is configured but not reachable."
        elif stream_configured and not stream_test_passed:
            stream_message = stream_test_message or "Run the stream test to prove the source password and mount accept audio."
        elif bool(output["icecast_enabled"]) and not bool(str(output["icecast_password"] or "").strip()):
            stream_message = "Enter the Icecast source password before testing stream output."
        else:
            stream_message = "Configure and test the live Icecast stream destination."

        qwen_ready = bool(dependencies["qwen_tts_runtime"].get("installed"))
        python_ready = bool(dependencies["python_runtime"].get("installed"))
        ai_runtime_ready = bool(ai_status.get("enabled")) and bool(ai_status.get("ready"))
        local_tts_ready = (
            (not bool(config.get("ai_enabled")))
            or bool(ai_status.get("local_tts_runtime_installed"))
            or (qwen_ready and python_ready)
        )
        live_ai_voice_ready = bool(
            ai_runtime_ready
            and bool(startup_ai.get("ready"))
            and str(ai_status.get("tts_provider") or "").strip() == DEFAULT_AI_TTS_PROVIDER
        )
        ai_test_passed = _truthy(station_settings.get(AI_TEST_PASSED_KEY, "false")) or live_ai_voice_ready
        ai_signature_verified = _stored_verified(station_settings, AI_VERIFIED_KEY, AI_SIGNATURE_KEY, ai_signature)
        ai_verified = (
            (not bool(config.get("ai_enabled")))
            or (
                ai_runtime_ready
                and ai_test_passed
                and (ai_signature_verified or live_ai_voice_ready or setup_completed_flag)
            )
        )

        webview_ready = bool(dependencies["webview2"].get("installed"))
        ollama_installed = bool(dependencies["ollama"].get("installed"))
        ollama_running = bool(ai_status.get("ollama_running"))

        checks = [
            _check(
                "backend",
                "Backend health",
                True,
                "Backend is responding to onboarding checks.",
            ),
            _check(
                "media_runtime",
                "Media runtime tools",
                media_ready,
                "FFmpeg, ffplay, and ffprobe are available." if media_ready else "Install or bootstrap FFmpeg, ffplay, and ffprobe.",
                ffmpeg=ffmpeg,
                ffplay=ffplay,
                ffprobe=ffprobe,
            ),
            _check(
                "webview2",
                "Desktop browser runtime",
                webview_ready,
                "WebView2 runtime is installed." if webview_ready else "Install the Microsoft WebView2 runtime before completing setup.",
                dependency=dependencies["webview2"],
            ),
            _check(
                "station",
                "Station profile",
                len(stations) >= 1 and bool(str(config["station_name"]).strip()),
                "Station identity is configured.",
                station_id=sid,
                station_name=config["station_name"],
            ),
            _check(
                "local_output",
                "Local monitor output",
                local_verified,
                (
                    "Local monitor output has been verified."
                    if local_verified
                    else "Verify local audio. If no device is selected, the system default output will be used."
                ),
                configured=local_configured,
                output_device_id=output["output_device_id"],
                signature=local_signature,
            ),
            _check(
                "stream_output",
                "Stream output",
                stream_verified,
                stream_message,
                configured=stream_configured,
                reachable=stream_reachable,
                host=output["icecast_host"],
                port=output["icecast_port"],
                mount=output["icecast_mount"],
                icecast_url=config["icecast_url"],
                source_user=output["icecast_user"],
                test_passed=stream_test_passed,
                test_message=stream_test_message,
                tested_at=str(station_settings.get(STREAM_TESTED_AT_KEY, "") or ""),
                stream_codec_profile=output["stream_codec_profile"],
                stream_bitrate_kbps=output["stream_bitrate_kbps"],
                signature=stream_signature,
            ),
            _check(
                "ollama",
                "Ollama runtime",
                (not bool(config["ai_enabled"])) or (ollama_installed and ollama_running),
                (
                    "Ollama is installed and responding."
                    if (ollama_installed and ollama_running)
                    else "Install Ollama and make sure the local service is running."
                )
                if bool(config["ai_enabled"])
                else "AI voice is disabled for this station.",
                installed=ollama_installed,
                running=ollama_running,
                dependency=dependencies["ollama"],
            ),
            _check(
                "local_tts_runtime",
                "Local Qwen TTS runtime",
                local_tts_ready,
                (
                    "Local Qwen TTS runtime is present and responding."
                    if local_tts_ready
                    else "Install the private Python runtime and Qwen TTS assets for local voice synthesis."
                )
                if bool(config["ai_enabled"])
                else "AI voice is disabled for this station.",
                qwen_tts_runtime=dependencies["qwen_tts_runtime"],
                python_runtime=dependencies["python_runtime"],
            ),
            _check(
                "ai_tts",
                "AI voice test",
                ai_verified,
                (
                    "AI voice has been warmed, tested, and verified."
                    if ai_verified
                    else (
                        str(station_settings.get(AI_TEST_MESSAGE_KEY, "") or "").strip()
                        or "Run the AI voice test, validate the selected warmth preset, then verify AI/TTS."
                    )
                ),
                runtime_ready=ai_runtime_ready,
                test_passed=ai_test_passed,
                status=ai_status,
                signature=ai_signature,
            ),
            _check(
                "startup_ai_buffer",
                "Startup AI buffer",
                bool(startup_ai["ready"]),
                str(startup_ai["message"]),
                state=startup_ai["state"],
                required_ready_track_intros=startup_ai["required_ready_track_intros"],
                ready_track_intros=startup_ai["ready_track_intros"],
                prefetch=startup_ai["prefetch"],
            ),
        ]

        required = _required_checks(config)
        for check in checks:
            is_required = str(check["name"]) in required
            check["required"] = is_required
            if not is_required and not bool(check["ready"]):
                check["status"] = "optional"
        ready_names = {str(check["name"]) for check in checks if bool(check["ready"])}
        can_complete = required.issubset(ready_names)
        setup_marked_complete = _truthy(station_settings.get(SETUP_COMPLETED_KEY, "false")) and can_complete
        blocking = [
            str(check["message"])
            for check in checks
            if str(check["name"]) in required and not bool(check["ready"])
        ]
        deployment_signature = _deployment_signature(sid, config, checks, required)
        certificate_signature = str(station_settings.get(DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY, "") or "")
        deployment_certified = (
            setup_marked_complete
            and _truthy(station_settings.get(DEPLOYMENT_CERTIFIED_KEY, "false"))
            and certificate_signature == deployment_signature
        )
        completed = deployment_certified
        deployment_certificate = {
            "certified": deployment_certified,
            "status": "certified" if deployment_certified else ("ready_to_certify" if can_complete else "blocked"),
            "certificate_id": str(station_settings.get(DEPLOYMENT_CERTIFICATE_ID_KEY, "") or ""),
            "certified_at": str(station_settings.get(DEPLOYMENT_CERTIFIED_AT_KEY, "") or ""),
            "signature": certificate_signature if deployment_certified else deployment_signature,
            "message": (
                "This station is certified for the verified output and AI configuration."
                if deployment_certified
                else (
                    "All checks passed. Finish setup to issue the deployment certificate."
                    if can_complete
                    else "Complete every required check before deployment certification."
                )
            ),
        }

        return {
            "station_id": sid,
            "station": {"id": sid, "name": str(station["name"] or "")},
            "config": config,
            "completed": completed,
            "can_complete": can_complete,
            "deployment_certificate": deployment_certificate,
            "required_checks": sorted(required),
            "checks": checks,
            "blocking_reasons": blocking,
            "last_successful_validation_time": station_settings.get(SETUP_LAST_VERIFIED_AT_KEY, ""),
            "last_configured_at": station_settings.get(SETUP_CONFIGURED_AT_KEY, ""),
            "codec_presets": CODEC_PRESETS,
            "warmth_presets": [
                {"id": "warm", "label": "Warm"},
                {"id": "energetic", "label": "Energetic"},
                {"id": "smooth", "label": "Smooth"},
                {"id": "professional", "label": "Professional"},
            ],
        }
    finally:
        conn.close()


def _find_check(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        (check for check in state.get("checks", []) if str(check.get("name")) == name),
        {},
    )


def _persist_setup_config(payload: SetupConfigurationPayload) -> int:
    if not bool(payload.local_output_enabled) and not bool(payload.icecast_enabled):
        raise HTTPException(status_code=400, detail="enable at least one output path")

    conn = get_connection()
    try:
        station_repo = StationRepository(conn)
        station, _stations = _station_row(conn, int(payload.station_id))
        sid = int(station["id"])
        station_repo.update_name(sid, str(payload.station_name or DEFAULT_STATION_NAME))

        profile, bitrate = _normalize_stream_profile(payload.stream_codec_profile, DEFAULT_STREAM_BITRATE_KBPS)
        parsed_url = _parse_icecast_url(payload.icecast_url)
        icecast_host = str(parsed_url.get("host") or payload.icecast_host or "127.0.0.1").strip()
        icecast_port = max(1, min(65535, _safe_int(parsed_url.get("port", payload.icecast_port), 8000)))
        icecast_mount = _normalize_mount(parsed_url.get("mount") or payload.icecast_mount)
        raw_user = str(payload.icecast_user or "").strip()
        parsed_user = str(parsed_url.get("user") or "").strip()
        icecast_user = parsed_user if parsed_user and (not raw_user or raw_user == "source") else (raw_user or "source")
        icecast_password = str(payload.icecast_password or parsed_url.get("password") or "")
        output_repo = StationOutputRepository(conn)
        output_repo.upsert(
            station_id=sid,
            local_output_enabled=bool(payload.local_output_enabled),
            output_device_id=str(payload.output_device_id or "").strip(),
            icecast_enabled=bool(payload.icecast_enabled),
            icecast_host=icecast_host,
            icecast_port=icecast_port,
            icecast_mount=icecast_mount,
            icecast_user=icecast_user,
            icecast_password=icecast_password,
            stream_codec_profile=profile,
            stream_bitrate_kbps=bitrate,
        )

        persona = _warmth_to_persona(payload.ai_warmth)
        managed_qwen_tts_path = _managed_qwen_tts_path()
        SettingsRepository(conn).upsert_station(
            sid,
            {
                "ai_host_enabled": str(bool(payload.ai_enabled)).lower(),
                "ai_tts_provider": DEFAULT_AI_TTS_PROVIDER,
                "ai_tts_model_path": managed_qwen_tts_path,
                "ai_llm_model": DEFAULT_AI_LLM_MODEL,
                "ai_voice_persona": persona,
                "icecast_host": icecast_host,
                "icecast_port": str(icecast_port),
                "icecast_mount": icecast_mount,
                "icecast_url": _compose_icecast_url(icecast_host, icecast_port, icecast_mount),
                "icecast_user": icecast_user,
                "icecast_username": icecast_user,
                "icecast_password": "",
                "icecast_stream_name": DEFAULT_STREAM_NAME,
                "stream_codec_profile": profile,
                "stream_bitrate_kbps": str(bitrate),
                "output_mode": "icecast" if bool(payload.icecast_enabled) else "speaker",
                "speaker_monitor_enabled": str(bool(payload.local_output_enabled)).lower(),
                AI_STACK_KEY: DEFAULT_AI_STACK,
                AI_WARMTH_KEY: _persona_to_warmth(persona),
                SETUP_CONFIGURED_AT_KEY: _now_iso(),
                SETUP_COMPLETED_KEY: "false",
                LOCAL_VERIFIED_KEY: "false",
                STREAM_VERIFIED_KEY: "false",
                STREAM_TEST_PASSED_KEY: "false",
                STREAM_TEST_MESSAGE_KEY: "",
                AI_VERIFIED_KEY: "false",
                AI_TEST_PASSED_KEY: "false",
                AI_TEST_MESSAGE_KEY: "",
            },
        )
        return sid
    finally:
        conn.close()


@router.get("/api/setup/state")
def get_setup_state(
    request: Request,
    station_id: int = 0,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    return _build_state(request, station_id)


@router.post("/api/setup/preflight")
def run_setup_preflight(
    payload: SetupStationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.view", "stations.edit")),
):
    return _build_state(request, payload.station_id)


@router.post("/api/setup/repair-dependencies")
def repair_setup_dependencies(
    payload: SetupStationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    from app.dependency_bootstrap import bootstrap_dependencies

    bootstrap_dependencies()
    return _build_state(request, payload.station_id)


@router.post("/api/setup/configure")
def configure_setup(
    payload: SetupConfigurationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    station_id = _persist_setup_config(payload)
    return _build_state(request, station_id)


@router.post("/api/setup/test-ai")
def test_ai_voice(
    payload: SetupStationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    state = _build_state(request, payload.station_id)
    config = dict(state.get("config") or {})
    conn = get_connection()
    try:
        settings_repo = SettingsRepository(conn)
        station_settings = settings_repo.get_station(int(state["station_id"]))
        result = _run_ai_voice_test_guarded(
            int(state["station_id"]),
            str(config.get("station_name") or DEFAULT_STATION_NAME),
            station_settings,
        )
        settings_repo.upsert_station(
            int(state["station_id"]),
            {
                AI_TEST_PASSED_KEY: str(bool(result.get("ok"))).lower(),
                AI_TEST_MESSAGE_KEY: str(result.get("message") or ""),
                AI_TESTED_AT_KEY: _now_iso(),
                AI_VERIFIED_KEY: "false",
                SETUP_COMPLETED_KEY: "false",
            },
        )
    finally:
        conn.close()
    return _build_state(request, int(state["station_id"]))


@router.post("/api/setup/verify")
def verify_setup_check(
    payload: SetupVerificationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    check_name = str(payload.check or "").strip()
    if check_name not in VERIFIABLE_CHECKS:
        raise HTTPException(status_code=400, detail="unsupported setup verification check")

    state = _build_state(request, payload.station_id)
    check = _find_check(state, check_name)
    details = dict(check.get("details") or {})

    if check_name == "local_output" and not bool(details.get("configured")):
        raise HTTPException(status_code=409, detail="local monitor output is not configured")
    if check_name == "stream_output" and not bool(details.get("configured")):
        raise HTTPException(status_code=409, detail="stream output is not configured")
    if check_name == "stream_output" and not bool(details.get("reachable")):
        raise HTTPException(status_code=409, detail="stream destination is not reachable")
    if check_name == "stream_output":
        # Setup state is deliberately redacted. Resolve the write-only
        # credential again only for the in-process publish test.
        conn = get_connection()
        try:
            station_id = int(state["station_id"])
            stream_output = _output_payload(
                conn,
                station_id,
                _settings(conn, station_id),
            )
        finally:
            conn.close()
        stream_test = _live_stream_output_verification(station_id)
        if stream_test is None:
            stream_test = _run_stream_output_test(stream_output)
        conn = get_connection()
        try:
            SettingsRepository(conn).upsert_station(
                int(state["station_id"]),
                {
                    STREAM_TEST_PASSED_KEY: str(bool(stream_test.get("ok"))).lower(),
                    STREAM_TEST_MESSAGE_KEY: str(stream_test.get("message") or ""),
                    STREAM_TESTED_AT_KEY: _now_iso(),
                    SETUP_COMPLETED_KEY: "false",
                    DEPLOYMENT_CERTIFIED_KEY: "false",
                    DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY: "",
                },
            )
        finally:
            conn.close()
        if not bool(stream_test.get("ok")):
            raise HTTPException(status_code=409, detail=str(stream_test.get("message") or "stream test failed"))
    if check_name == "ai_tts" and not bool(details.get("runtime_ready")):
        raise HTTPException(status_code=409, detail="AI/TTS runtime is not ready")
    if check_name == "ai_tts" and not bool(details.get("test_passed")):
        raise HTTPException(status_code=409, detail="run the AI voice test before verifying AI/TTS")

    signature = str(details.get("signature") or "")
    key_map = {
        "local_output": (LOCAL_VERIFIED_KEY, LOCAL_SIGNATURE_KEY),
        "stream_output": (STREAM_VERIFIED_KEY, STREAM_SIGNATURE_KEY),
        "ai_tts": (AI_VERIFIED_KEY, AI_SIGNATURE_KEY),
    }
    flag_key, signature_key = key_map[check_name]

    conn = get_connection()
    try:
        SettingsRepository(conn).upsert_station(
            int(state["station_id"]),
            {
                flag_key: "true",
                signature_key: signature,
                SETUP_LAST_VERIFIED_AT_KEY: _now_iso(),
                SETUP_COMPLETED_KEY: "false",
                DEPLOYMENT_CERTIFIED_KEY: "false",
                DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY: "",
            },
        )
    finally:
        conn.close()

    return _build_state(request, int(state["station_id"]))


@router.post("/api/setup/complete")
def complete_setup(
    payload: SetupStationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    state = _build_state(request, payload.station_id)
    if not bool(state.get("can_complete")):
        raise HTTPException(status_code=409, detail={"message": "setup is not ready", "state": state})

    conn = get_connection()
    try:
        startup_ai = dict((state.get("config") or {}).get("startup_ai_readiness") or {})
        required = {str(name) for name in state.get("required_checks", [])}
        certificate_signature = _deployment_signature(
            int(state["station_id"]),
            dict(state.get("config") or {}),
            list(state.get("checks") or []),
            required,
        )
        certified_at = _now_iso()
        certificate_id = f"RBW-{int(state['station_id'])}-{certified_at[:10].replace('-', '')}-{certificate_signature[:8].upper()}"
        SettingsRepository(conn).upsert_station(
            int(state["station_id"]),
            {
                SETUP_COMPLETED_KEY: "true",
                SETUP_LAST_VERIFIED_AT_KEY: _now_iso(),
                STARTUP_AI_STATE_KEY: str(startup_ai.get("state") or ""),
                STARTUP_AI_READY_INTROS_KEY: str(startup_ai.get("ready_track_intros") or 0),
                STARTUP_AI_REQUIRED_INTROS_KEY: str(startup_ai.get("required_ready_track_intros") or 0),
                DEPLOYMENT_CERTIFIED_KEY: "true",
                DEPLOYMENT_CERTIFIED_AT_KEY: certified_at,
                DEPLOYMENT_CERTIFICATE_ID_KEY: certificate_id,
                DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY: certificate_signature,
            },
        )
    finally:
        conn.close()

    return _build_state(request, int(state["station_id"]))


@router.post("/api/setup/reset")
def reset_setup(
    payload: SetupStationPayload,
    request: Request,
    _user=Depends(require_any_permission("stations.edit")),
):
    conn = get_connection()
    try:
        SettingsRepository(conn).upsert_station(
            int(payload.station_id),
            {
                SETUP_COMPLETED_KEY: "false",
                LOCAL_VERIFIED_KEY: "false",
                STREAM_VERIFIED_KEY: "false",
                STREAM_TEST_PASSED_KEY: "false",
                STREAM_TEST_MESSAGE_KEY: "",
                AI_VERIFIED_KEY: "false",
                AI_TEST_PASSED_KEY: "false",
                AI_TEST_MESSAGE_KEY: "",
                DEPLOYMENT_CERTIFIED_KEY: "false",
                DEPLOYMENT_CERTIFICATE_SIGNATURE_KEY: "",
            },
        )
    finally:
        conn.close()

    return _build_state(request, payload.station_id)
