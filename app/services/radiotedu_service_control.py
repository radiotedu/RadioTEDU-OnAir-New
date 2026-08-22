from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from fastapi import HTTPException

from app.runtime_paths import get_data_dir
from app.dependency_bootstrap import managed_binary_path
from app.reliability import atomic_write_json, read_json_object


SETTINGS_KEY = "radiotedu_service_control_v1"
CONFIRMATIONS = {
    "start": "START SERVICE",
    "stop": "STOP SERVICE",
    "restart": "RESTART SERVICE",
    "update_database": "UPDATE DATABASE",
    "update_repository": "UPDATE REPOSITORY",
    "pull_model": "INSTALL MODEL",
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SCM_OWNED_SERVICE_IDS = frozenset({"juke_media_agent", "voting_agent"})
STARTUP_OWNER_ONAIR_PROCESS = "onair_process"
STARTUP_OWNER_WINDOWS_SCM = "windows_scm"
SECRET_KEY_PARTS = ("secret", "password", "token", "credential", "authorization")
_LOCK = threading.RLock()
_PROCESSES: dict[str, subprocess.Popen] = {}

SERVICE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "ollama_runtime": {
        "product": "RadioTEDU AI",
        "name": "Ollama Model Runtime",
        "description": "Local, optional language-model runtime. Music and live broadcasting remain independent.",
        "kind": "ollama",
        "required": (),
        "default_health_urls": ("http://127.0.0.1:11434/api/tags",),
        "mounts": (),
        "database_supported": False,
        "database_kind": "",
    },
    "rtai_shared_ai": {
        "product": "RadioTEDU AI Radio",
        "name": "Shared AI (Qwen)",
        "description": "Shared local speech engine used by both AI stations.",
        "kind": "rtai_service",
        "service_name": "RadioTEDU.SharedAI",
        "required": ("packaging/broadcast/run-service.ps1",),
        "default_health_urls": ("http://127.0.0.1:8090/health",),
        "mounts": (),
        "database_supported": False,
        "database_kind": "",
    },
    "rtai_supervisor": {
        "product": "RadioTEDU AI Radio",
        "name": "EN + FR Broadcast Supervisor",
        "description": "Supervises the independent English and French AI stations.",
        "kind": "rtai_service",
        "service_name": "RadioTEDU.BroadcastSupervisor",
        "required": (
            "packaging/broadcast/run-service.ps1",
            "scripts/run_station_forever.py",
        ),
        "default_health_urls": (
            "http://127.0.0.1:8765/health",
            "http://127.0.0.1:8766/health",
        ),
        "mounts": ("/en", "/fr"),
        "database_supported": True,
        "database_kind": "SQLite",
    },
    "voting_agent": {
        "product": "RadioTEDU Voting",
        "name": "Local Voting Agent",
        "description": "Local voting playout agent and canonical WSS client.",
        "kind": "node",
        "entry": "scripts/voting-supervisor.mjs",
        "required": ("scripts/voting-supervisor.mjs", "package.json"),
        "default_health_urls": (
            "http://127.0.0.1:4317/api/health",
            "http://127.0.0.1:4317/api/state",
        ),
        # Voting changes the selected genre/program timeline; it does not own
        # a source mount. Keeping the old /ai claim here conflicted with the
        # independently supervised English and French AI stations.
        "mounts": (),
        "startup_owner": STARTUP_OWNER_WINDOWS_SCM,
        "database_supported": False,
        "database_kind": "",
    },
    "voting_backend": {
        "product": "RadioTEDU Voting",
        "name": "Voting Web Backend",
        "description": "Public voting API, PostgreSQL database, and Redis state.",
        "kind": "node",
        "entry": "dist/server.js",
        "required": ("dist/server.js", "package.json"),
        "default_health_urls": (
            "https://radiotedu.com/jukebox/api/v1/next-song-voting/status",
            "https://radiotedu.com/jukebox/api/v1/next-song-voting/rounds/active",
        ),
        "mounts": (),
        "database_supported": True,
        "database_kind": "PostgreSQL",
        "migrations": ("db:migrate", "db:migrate:voting-agent"),
    },
    "juke_media_agent": {
        "product": "RadioTEDU Juke",
        "name": "Juke Local Media Agent",
        "description": "Loopback media agent for local library and playout health.",
        "kind": "node",
        "entry": "server.js",
        "required": ("server.js", "package.json"),
        "default_health_urls": (
            "http://127.0.0.1:3210/v1/health",
            "http://127.0.0.1:3210/v1/status",
        ),
        "mounts": (),
        "startup_owner": STARTUP_OWNER_WINDOWS_SCM,
        "database_supported": False,
        "database_kind": "",
    },
    "juke_backend": {
        "product": "RadioTEDU Juke",
        "name": "Juke Web Backend",
        "description": "Public Juke API, PostgreSQL database, and Redis state.",
        "kind": "node",
        "entry": "dist/server.js",
        "required": ("dist/server.js", "package.json"),
        "default_health_urls": ("https://radiotedu.com/juke-local",),
        "mounts": (),
        "database_supported": True,
        "database_kind": "PostgreSQL",
        "migrations": ("db:migrate",),
    },
}


def default_settings() -> dict[str, dict[str, Any]]:
    return {
        service_id: {
            "enabled": False,
            "auto_start": False,
            "source_dir": "",
            "config_path": "",
            "health_urls": list(definition["default_health_urls"]),
            "database_backup_dir": "",
        }
        for service_id, definition in SERVICE_DEFINITIONS.items()
    }


def load_settings(raw: str | None) -> dict[str, dict[str, Any]]:
    defaults = default_settings()
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return defaults
    if not isinstance(parsed, dict):
        return defaults
    for service_id, default in defaults.items():
        value = parsed.get(service_id)
        if not isinstance(value, dict):
            continue
        default.update(
            {
                "enabled": bool(value.get("enabled", False)),
                "auto_start": bool(value.get("auto_start", False)),
                "source_dir": str(value.get("source_dir") or "").strip(),
                "config_path": str(value.get("config_path") or "").strip(),
                "health_urls": [
                    str(item).strip()
                    for item in list(value.get("health_urls") or [])
                    if str(item).strip()
                ][:8],
                "database_backup_dir": str(
                    value.get("database_backup_dir") or ""
                ).strip(),
            }
        )
    return defaults


def _absolute_path(raw: Any, *, field: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"{field}_must_be_absolute")
    return str(path.resolve(strict=False))


def _validated_health_url(raw: Any) -> str:
    value = str(raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid_health_url")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="non_loopback_health_url_requires_https",
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(status_code=400, detail="unsafe_health_url")
    return value


def normalize_settings(payload: Any) -> dict[str, dict[str, Any]]:
    incoming = payload if isinstance(payload, dict) else {}
    unknown = set(incoming) - set(SERVICE_DEFINITIONS)
    if unknown:
        raise HTTPException(status_code=400, detail="unknown_service")
    output = default_settings()
    for service_id in SERVICE_DEFINITIONS:
        value = incoming.get(service_id, {})
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="invalid_service_settings")
        urls = list(value.get("health_urls") or [])
        if len(urls) > 8:
            raise HTTPException(status_code=400, detail="too_many_health_urls")
        output[service_id] = {
            "enabled": bool(value.get("enabled", False)),
            "auto_start": bool(value.get("auto_start", False)),
            "source_dir": _absolute_path(
                value.get("source_dir"),
                field=f"{service_id}_source_dir",
            ),
            "config_path": _absolute_path(
                value.get("config_path"),
                field=f"{service_id}_config_path",
            ),
            "health_urls": [
                _validated_health_url(item) for item in urls if str(item).strip()
            ],
            "database_backup_dir": _absolute_path(
                value.get("database_backup_dir"),
                field=f"{service_id}_database_backup_dir",
            ),
        }
    return output


def settings_json(settings: dict[str, dict[str, Any]]) -> str:
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


def _runtime_dir() -> Path:
    path = get_data_dir() / "radiotedu-services"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_path() -> Path:
    return _runtime_dir() / "processes.json"


def _process_registry_key(service_id: str) -> str:
    # The data root can differ between isolated app instances and tests.
    # Never let an in-memory process from one instance shadow another.
    return f"{_runtime_dir().resolve()}::{service_id}"


def _maintenance_path() -> Path:
    return _runtime_dir() / "database-maintenance.json"


def _foreground_verification_path() -> Path:
    return _runtime_dir() / "foreground-verifications.json"


def _load_foreground_verifications() -> dict[str, dict[str, Any]]:
    value = read_json_object(_foreground_verification_path())
    return value if isinstance(value, dict) else {}


def _save_foreground_verifications(value: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(_foreground_verification_path(), value)


def _startup_owner(service_id: str) -> str:
    if service_id in SCM_OWNED_SERVICE_IDS:
        return STARTUP_OWNER_WINDOWS_SCM
    return str(
        SERVICE_DEFINITIONS[service_id].get(
            "startup_owner", STARTUP_OWNER_ONAIR_PROCESS
        )
    )


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _config_fingerprint(config: dict[str, Any]) -> str:
    """Return a content fingerprint without retaining configuration values."""
    path = Path(str(config.get("config_path") or ""))
    try:
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        if path.is_dir():
            entries = [
                (str(item.relative_to(path)).replace("\\", "/"), item.stat().st_size, item.stat().st_mtime_ns)
                for item in sorted(path.rglob("*"))
                if item.is_file()
            ]
            return _hash_json(entries)
    except OSError:
        return ""
    return ""


def _source_fingerprint(service_id: str, config: dict[str, Any]) -> str:
    """Fingerprint source identity without exposing its local path or contents."""
    source = _source_status(service_id, config)
    raw_root = str(config.get("source_dir") or "").strip()
    root = Path(raw_root)
    required: list[dict[str, Any]] = []
    for relative in SERVICE_DEFINITIONS[service_id].get("required", ()):
        item = root / relative
        try:
            stat = item.stat()
            required.append(
                {
                    "name": str(relative),
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            )
        except OSError:
            required.append({"name": str(relative), "missing": True})
    return _hash_json(
        {
            "root": hashlib.sha256(str(root.resolve(strict=False)).encode()).hexdigest(),
            "commit": str(source.get("commit") or ""),
            "dirty": bool(source.get("dirty")),
            "required": required,
        }
    )


def _verification_fingerprints(
    service_id: str, config: dict[str, Any]
) -> tuple[str, str]:
    return _source_fingerprint(service_id, config), _config_fingerprint(config)


def _env_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(value: Any) -> bool:
    return str(value or "").strip().lower() in LOOPBACK_HOSTS


def _env_port(values: dict[str, str], key: str) -> int:
    try:
        value = int(str(values.get(key) or "").strip())
    except (TypeError, ValueError):
        return 0
    return value if 1 <= value <= 65535 else 0


def _autonomous_startup_requirements(
    service_id: str, config: dict[str, Any]
) -> list[str]:
    """Return stable, non-secret configuration failures for SCM enrollment."""
    if _startup_owner(service_id) != STARTUP_OWNER_WINDOWS_SCM:
        return []
    path = Path(str(config.get("config_path") or ""))
    if not path.is_file():
        return ["protected_config_not_ready"]
    try:
        values = _read_env(path)
    except OSError:
        return ["protected_config_unreadable"]
    if service_id == "juke_media_agent":
        failures: list[str] = []
        if not _is_loopback_host(values.get("MEDIA_AGENT_BIND_HOST")):
            failures.append("juke_bind_not_loopback")
        if str(values.get("AI_MIRROR_ENABLED") or "").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }:
            failures.append("juke_ai_mirror_enabled")
        if str(values.get("AI_AUTOPLAY_ENABLED") or "").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }:
            failures.append("juke_ai_autoplay_enabled")
        return failures
    if service_id == "voting_agent":
        failures = []
        if _env_port(values, "PORT") != 4317:
            failures.append("voting_control_port_not_4317")
        if not _env_truthy(values.get("LOCAL_HTTP_STREAM_ENABLED")):
            failures.append("voting_local_stream_disabled")
        if _env_port(values, "LOCAL_HTTP_STREAM_PORT") != 4320:
            failures.append("voting_stream_port_not_4320")
        return failures
    return []


def _foreground_evidence(
    service_id: str, evidence: Any
) -> tuple[dict[str, Any], list[str]]:
    """Allowlist foreground proof so caller input can never become a secret ledger."""
    raw = evidence if isinstance(evidence, dict) else {}
    raw_ports = raw.get("loopback_ports")
    raw_ports = raw_ports if isinstance(raw_ports, list) else []
    ports = sorted(
        {
            int(item)
            for item in raw_ports
            if isinstance(item, int)
            and 1 <= int(item) <= 65535
        }
    )
    if service_id == "juke_media_agent":
        public = {
            "loopback_ports": [port for port in ports if port == 3210],
            "mirror_disabled": raw.get("mirror_disabled") is True,
            "autoplay_disabled": raw.get("autoplay_disabled") is True,
        }
        failures = []
        if public["loopback_ports"] != [3210]:
            failures.append("juke_loopback_port_unverified")
        if not public["mirror_disabled"]:
            failures.append("juke_mirror_unverified")
        if not public["autoplay_disabled"]:
            failures.append("juke_autoplay_unverified")
        return public, failures
    if service_id == "voting_agent":
        public = {
            "loopback_ports": [port for port in ports if port in {4317, 4320}],
            "ai_mount_owner": (
                "voting_agent" if raw.get("ai_mount_owner") == "voting_agent" else ""
            ),
        }
        failures = []
        if public["loopback_ports"] != [4317, 4320]:
            failures.append("voting_loopback_ports_unverified")
        if public["ai_mount_owner"] != "voting_agent":
            failures.append("voting_ai_mount_owner_unverified")
        return public, failures
    return {}, []


def invalidate_stale_foreground_verification(
    service_id: str, config: dict[str, Any]
) -> bool:
    """Drop verification evidence when the source or protected config changes."""
    if _startup_owner(service_id) != STARTUP_OWNER_WINDOWS_SCM:
        return False
    source_fingerprint, config_fingerprint = _verification_fingerprints(service_id, config)
    with _LOCK:
        ledger = _load_foreground_verifications()
        record = ledger.get(service_id)
        if not isinstance(record, dict):
            return False
        if (
            record.get("source_fingerprint") == source_fingerprint
            and record.get("config_fingerprint") == config_fingerprint
        ):
            return False
        ledger.pop(service_id, None)
        _save_foreground_verifications(ledger)
        return True


def record_successful_foreground_evidence(
    service_id: str,
    config: dict[str, Any],
    evidence: Any,
) -> dict[str, Any]:
    """Record an allowlisted foreground proof for future SCM autonomous startup."""
    if service_id not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="unknown_service")
    if _startup_owner(service_id) != STARTUP_OWNER_WINDOWS_SCM:
        raise HTTPException(status_code=409, detail="service_not_windows_scm_owned")
    source = _source_status(service_id, config)
    requirements = _autonomous_startup_requirements(service_id, config)
    public_evidence, evidence_failures = _foreground_evidence(service_id, evidence)
    if not source["ready"] or requirements or evidence_failures:
        raise HTTPException(status_code=409, detail="foreground_verification_failed")
    source_fingerprint, config_fingerprint = _verification_fingerprints(service_id, config)
    verified_at = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        ledger = _load_foreground_verifications()
        ledger[service_id] = {
            "source_fingerprint": source_fingerprint,
            "config_fingerprint": config_fingerprint,
            "verified_at": verified_at,
            "evidence": public_evidence,
        }
        _save_foreground_verifications(ledger)
    return autonomous_startup_status(service_id, config)


def autonomous_startup_status(
    service_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    owner = _startup_owner(service_id)
    if owner != STARTUP_OWNER_WINDOWS_SCM:
        return {
            "owner": owner,
            "ready": False,
            "state": "not_windows_scm_owned",
            "reasons": [],
            "verified_at": "",
        }
    source = _source_status(service_id, config)
    requirements = _autonomous_startup_requirements(service_id, config)
    if not source["ready"] or requirements:
        return {
            "owner": owner,
            "ready": False,
            "state": "not_ready",
            "reasons": (["service_source_not_ready"] if not source["ready"] else [])
            + requirements,
            "verified_at": "",
        }
    stale = invalidate_stale_foreground_verification(service_id, config)
    with _LOCK:
        record = _load_foreground_verifications().get(service_id)
    if not isinstance(record, dict):
        return {
            "owner": owner,
            "ready": False,
            "state": "verification_stale" if stale else "verification_required",
            "reasons": [
                "foreground_verification_stale"
                if stale
                else "foreground_verification_required"
            ],
            "verified_at": "",
        }
    return {
        "owner": owner,
        "ready": True,
        "state": "verified",
        "reasons": [],
        "verified_at": str(record.get("verified_at") or ""),
        "evidence": dict(record.get("evidence") or {}),
    }


def _load_ledger() -> dict[str, dict[str, Any]]:
    value = read_json_object(_ledger_path())
    return value if isinstance(value, dict) else {}


def _save_ledger(value: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(_ledger_path(), value)


def _load_maintenance() -> dict[str, dict[str, Any]]:
    value = read_json_object(_maintenance_path())
    return value if isinstance(value, dict) else {}


def _save_maintenance(value: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(_maintenance_path(), value)


def _record_database_maintenance(
    service_id: str, result: dict[str, Any]
) -> None:
    backup_files: list[str] = []
    if str(result.get("backup_file") or ""):
        backup_files.append(str(result["backup_file"]))
    station_results: list[dict[str, Any]] = []
    for station in list(result.get("stations") or []):
        if not isinstance(station, dict):
            continue
        backup = str(station.get("backup_file") or "")
        if backup:
            backup_files.append(backup)
        station_results.append(
            {
                "station_id": str(station.get("station_id") or ""),
                "tracks_found": int(station.get("tracks_found") or 0),
                "backup_created": bool(backup),
            }
        )
    with _LOCK:
        maintenance = _load_maintenance()
        maintenance[service_id] = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "backup_files": backup_files,
            "migrations_applied": int(result.get("migrations_applied") or 0),
            "stations": station_results,
        }
        _save_maintenance(maintenance)


def _database_status(
    service_id: str,
    config: dict[str, Any],
    *,
    source_ready: bool,
    config_ready: bool,
) -> dict[str, Any]:
    definition = SERVICE_DEFINITIONS[service_id]
    supported = bool(definition["database_supported"])
    if not supported:
        return {"supported": False, "state": "not_supported", "kind": ""}
    backup_raw = str(config.get("database_backup_dir") or "").strip()
    backup_path = Path(backup_raw) if backup_raw else None
    backup_ready = bool(backup_path and backup_path.is_absolute())
    ready = bool(source_ready and config_ready and backup_ready)
    record = _load_maintenance().get(service_id) or {}
    completed_at = str(record.get("completed_at") or "")
    return {
        "supported": True,
        "kind": str(definition.get("database_kind") or ""),
        "state": "updated" if completed_at and ready else "ready" if ready else "not_ready",
        "backup_configured": backup_ready,
        "last_update_at": completed_at,
        "last_backup_files": [
            str(item) for item in list(record.get("backup_files") or []) if str(item)
        ],
        "migrations_applied": int(record.get("migrations_applied") or 0),
        "stations": [
            item
            for item in list(record.get("stations") or [])
            if isinstance(item, dict)
        ],
    }


def _process_details_windows(pid: int) -> dict[str, Any] | None:
    # Process tracking is part of the safety boundary for managed services: a
    # PID is never trusted unless its creation time, executable, and command
    # line can be fingerprinted. Starting a fresh powershell.exe process for
    # that lookup is both slow and racy on a busy broadcast PC; the child can
    # be healthy while the auxiliary shell is still starting. Query Windows
    # directly first so this safety check has no helper-process dependency.
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if handle:
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    created_ticks = (
                        int(created.dwHighDateTime) << 32
                    ) | int(created.dwLowDateTime)
                    buffer = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(buffer))
                    executable = ""
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, buffer, ctypes.byref(length)
                    ):
                        executable = buffer.value
                    return {
                        "ProcessId": int(pid),
                        "CreationDate": str(created_ticks),
                        "ExecutablePath": executable,
                        "CommandLine": "",
                    }
            finally:
                kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    # Optional library fallback for restricted Windows configurations.
    try:
        import psutil
    except ImportError:  # pragma: no cover - packaged requirements include it
        psutil = None

    if psutil is not None:
        try:
            process = psutil.Process(int(pid))
            with process.oneshot():
                created = datetime.fromtimestamp(
                    process.create_time(), tz=timezone.utc
                ).isoformat()
                executable = process.exe()
                command = subprocess.list2cmdline(process.cmdline())
            return {
                "ProcessId": int(pid),
                "CreationDate": created,
                "ExecutablePath": executable,
                "CommandLine": command,
            }
        except (psutil.Error, OSError, ValueError):
            # Keep the older lookup as a compatibility fallback for restricted
            # Windows environments where process inspection is denied.
            pass

    script = (
        "$p=Get-Process -Id "
        + str(int(pid))
        + " -ErrorAction SilentlyContinue;"
        + "if($p){[pscustomobject]@{"
        + "ProcessId=$p.Id;"
        + "CreationDate=$p.StartTime.ToUniversalTime().ToString('o');"
        + "ExecutablePath=$p.Path;"
        + "CommandLine=''"
        + "}|ConvertTo-Json -Compress}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        parsed = json.loads(result.stdout.strip()) if result.stdout.strip() else None
        return parsed if isinstance(parsed, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _process_details_posix(pid: int) -> dict[str, Any] | None:
    root = Path("/proc") / str(int(pid))
    try:
        command = (root / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
        executable = os.readlink(root / "exe")
        created = str(root.stat().st_ctime_ns)
    except OSError:
        return None
    return {
        "ProcessId": int(pid),
        "CreationDate": created,
        "ExecutablePath": executable,
        "CommandLine": command,
    }


def _process_details(pid: int) -> dict[str, Any] | None:
    if int(pid) <= 0:
        return None
    if os.name == "nt":
        return _process_details_windows(pid)
    return _process_details_posix(pid)


def _process_fingerprint(details: dict[str, Any]) -> str:
    value = "\n".join(
        [
            str(details.get("CreationDate") or ""),
            str(details.get("ExecutablePath") or "").lower(),
            str(details.get("CommandLine") or ""),
        ]
    )
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _tracked_process(service_id: str) -> tuple[str, int | None]:
    with _LOCK:
        process_key = _process_registry_key(service_id)
        proc = _PROCESSES.get(process_key)
        if proc is not None:
            if proc.poll() is None:
                return "running", int(proc.pid)
            _PROCESSES.pop(process_key, None)
        ledger = _load_ledger()
        record = ledger.get(service_id)
        if not isinstance(record, dict):
            return "stopped", None
        pid = int(record.get("pid") or 0)
        details = _process_details(pid)
        if details and _process_fingerprint(details) == str(
            record.get("fingerprint") or ""
        ):
            return "running", pid
        ledger.pop(service_id, None)
        _save_ledger(ledger)
        return "stopped", None


def _ollama_executable() -> Path | None:
    candidates = [
        Path(value)
        for value in (
            os.getenv("RADIOTEDU_OLLAMA_EXE", "").strip(),
            shutil.which("ollama.exe") or shutil.which("ollama") or "",
            str(managed_binary_path("ollama.exe")),
            str(
                Path(os.getenv("LOCALAPPDATA", ""))
                / "Programs"
                / "Ollama"
                / "ollama.exe"
            )
            if os.getenv("LOCALAPPDATA", "").strip()
            else "",
        )
        if value
    ]
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=False)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _source_status(
    service_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    definition = SERVICE_DEFINITIONS[service_id]
    if definition["kind"] == "ollama":
        executable = _ollama_executable()
        return {
            "configured": True,
            "ready": executable is not None,
            "commit": "",
            "dirty": False,
            "missing": [] if executable else ["ollama.exe"],
            "executable": str(executable or ""),
        }
    raw = str(settings.get("source_dir") or "")
    if not raw:
        return {
            "configured": False,
            "ready": False,
            "commit": "",
            "dirty": False,
            "missing": list(definition["required"]),
        }
    root = Path(raw)
    missing = [
        relative
        for relative in definition["required"]
        if not (root / relative).is_file()
    ]
    commit = ""
    dirty = False
    if (root / ".git").exists():
        try:
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=5,
            ).stdout.strip()
            dirty = (
                subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                    timeout=5,
                ).stdout.strip()
                != ""
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "configured": True,
        "ready": root.is_dir() and not missing,
        "commit": commit,
        "dirty": dirty,
        "missing": missing,
    }


def _service_mounts(service_id: str, config: dict[str, Any]) -> list[str]:
    mounts = list(SERVICE_DEFINITIONS[service_id]["mounts"])
    if service_id != "juke_media_agent":
        return mounts
    env_path = Path(str(config.get("config_path") or ""))
    if not env_path.is_file():
        return mounts
    try:
        values = _read_env(env_path)
    except OSError:
        return mounts
    if str(values.get("AI_MIRROR_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        mounts.append("/ai")
    return mounts


def _sanitize_health(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_sanitize_health(item, depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            safe_key = str(key)[:80]
            if any(part in safe_key.lower() for part in SECRET_KEY_PARTS):
                continue
            output[safe_key] = _sanitize_health(item, depth + 1)
        return output
    return str(value)[:240]


def _juke_health_headers(
    url: str,
    config: dict[str, Any],
) -> dict[str, str]:
    config_path = Path(str(config.get("config_path") or ""))
    if not config_path.is_file():
        return {}
    secret = _read_env(config_path).get("MEDIA_AGENT_REQUEST_SECRET", "")
    if not secret:
        return {}
    parsed = urlparse(url)
    resource = parsed.path or "/"
    if parsed.query:
        resource = f"{resource}?{parsed.query}"
    timestamp = str(int(time.time()))
    message = f"GET\n{resource}\n{timestamp}\n".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), message, hashlib.sha256).digest()
    ).decode().rstrip("=")
    return {
        "X-Juke-Timestamp": timestamp,
        "X-Juke-Signature": signature,
    }


def _health_check(
    url: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    request = Request(
        _validated_health_url(url),
        headers={
            "Accept": "application/json",
            "User-Agent": "RadioTEDU-OnAir-ServiceControl/1.0",
            **dict(headers or {}),
        },
    )
    try:
        with urlopen(request, timeout=3.5) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            body = response.read(256_001)
            if len(body) > 256_000:
                raise ValueError("health_response_too_large")
            parsed = (
                json.loads(body.decode("utf-8", errors="replace"))
                if "json" in content_type.lower() and body
                else {}
            )
            return {
                "url": url,
                "ok": 200 <= int(response.status) < 400,
                "status": int(response.status),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "signals": _sanitize_health(parsed),
            }
    except HTTPError as exc:
        return {
            "url": url,
            "ok": False,
            "status": int(exc.code),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": "service_rejected_health_check",
        }
    except (URLError, TimeoutError, OSError, ValueError):
        return {
            "url": url,
            "ok": False,
            "status": 0,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": "service_unavailable",
        }


def service_status(
    service_id: str,
    settings: dict[str, dict[str, Any]],
    *,
    include_health: bool = True,
) -> dict[str, Any]:
    if service_id not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="unknown_service")
    definition = SERVICE_DEFINITIONS[service_id]
    config = settings[service_id]
    runtime, pid = _tracked_process(service_id)
    checks = (
        [
            (
                _health_check(url, _juke_health_headers(url, config))
                if service_id == "juke_media_agent"
                else _health_check(url)
            )
            for url in config["health_urls"]
        ]
        if include_health and config["enabled"]
        else []
    )
    source = _source_status(service_id, config)
    config_path = Path(str(config.get("config_path") or ""))
    config_ready = definition["kind"] == "ollama" or (
        bool(str(config.get("config_path") or ""))
        and (
            config_path.is_dir()
            if definition["kind"] == "rtai_service"
            else config_path.is_file()
        )
    )
    if (
        definition["kind"] == "ollama"
        and runtime == "stopped"
        and checks
        and all(item["ok"] for item in checks)
    ):
        runtime = "external"
    if not config["enabled"]:
        state = "disabled"
    elif checks and all(item["ok"] for item in checks):
        state = "healthy"
    elif runtime == "running":
        state = "degraded"
    elif source["ready"] and config_ready:
        state = "ready"
    else:
        state = "not_ready"
    return {
        "id": service_id,
        "product": definition["product"],
        "name": definition["name"],
        "description": definition["description"],
        "enabled": bool(config["enabled"]),
        "auto_start": bool(config["auto_start"]),
        "state": state,
        "runtime": runtime,
        "pid": pid,
        "source": source,
        "config_ready": config_ready,
        "health": checks,
        "mounts": _service_mounts(service_id, config),
        "startup_owner": _startup_owner(service_id),
        "autonomous_startup": autonomous_startup_status(service_id, config),
        "database_supported": bool(definition["database_supported"]),
        "database": _database_status(
            service_id,
            config,
            source_ready=bool(source["ready"]),
            config_ready=config_ready,
        ),
    }


def all_service_statuses(
    settings: dict[str, dict[str, Any]],
    *,
    include_health: bool = True,
) -> list[dict[str, Any]]:
    return [
        service_status(
            service_id,
            settings,
            include_health=include_health,
        )
        for service_id in SERVICE_DEFINITIONS
    ]


def public_settings(settings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "services": settings,
        "autonomous_startup": {
            service_id: autonomous_startup_status(service_id, settings[service_id])
            for service_id in SERVICE_DEFINITIONS
        },
        "definitions": [
            {
                "id": service_id,
                "product": definition["product"],
                "name": definition["name"],
                "description": definition["description"],
                "kind": definition["kind"],
                "startup_owner": _startup_owner(service_id),
                "database_supported": bool(
                    definition["database_supported"]
                ),
                "database_kind": str(definition.get("database_kind") or ""),
                "mounts": list(definition["mounts"]),
            }
            for service_id, definition in SERVICE_DEFINITIONS.items()
        ],
    }


def _build_command(
    service_id: str, config: dict[str, Any]
) -> tuple[list[str], Path]:
    definition = SERVICE_DEFINITIONS[service_id]
    if definition["kind"] == "ollama":
        executable = _ollama_executable()
        if not executable:
            raise HTTPException(status_code=409, detail="ollama_not_installed")
        return [str(executable), "serve"], executable.parent
    source = Path(config["source_dir"])
    if definition["kind"] == "rtai_service":
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            raise HTTPException(status_code=409, detail="powershell_not_found")
        repository_python = source / ".venv" / "Scripts" / "python.exe"
        python_executable = (
            str(repository_python) if repository_python.is_file() else sys.executable
        )
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(source / "packaging/broadcast/run-service.ps1"),
            "-ServiceName",
            str(definition["service_name"]),
            "-ProjectRoot",
            str(source),
            "-ConfigRoot",
            str(config["config_path"]),
            "-Python",
            python_executable,
        ]
        return command, source
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise HTTPException(status_code=409, detail="node_not_found")
    command = [
        node,
        f"--env-file={config['config_path']}",
        str(definition["entry"]),
    ]
    return command, source


def _mount_conflict(
    service_id: str, settings: dict[str, dict[str, Any]]
) -> str | None:
    requested = set(_service_mounts(service_id, settings[service_id]))
    if not requested:
        return None
    for other_id in SERVICE_DEFINITIONS:
        if other_id == service_id or not requested.intersection(
            _service_mounts(other_id, settings[other_id])
        ):
            continue
        runtime, _pid = _tracked_process(other_id)
        if runtime == "running":
            return other_id
    return None


def _start(
    service_id: str, settings: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    config = settings[service_id]
    if not config["enabled"]:
        raise HTTPException(status_code=409, detail="service_disabled")
    source = _source_status(service_id, config)
    if not source["ready"]:
        raise HTTPException(status_code=409, detail="service_source_not_ready")
    definition = SERVICE_DEFINITIONS[service_id]
    if definition["kind"] == "ollama":
        current = service_status(service_id, settings, include_health=True)
        if current["runtime"] == "external":
            raise HTTPException(
                status_code=409,
                detail="ollama_already_running_outside_onair",
            )
    config_path = Path(config["config_path"])
    config_ready = definition["kind"] == "ollama" or (
        config_path.is_dir()
        if definition["kind"] == "rtai_service"
        else config_path.is_file()
    )
    if not config_ready:
        raise HTTPException(status_code=409, detail="service_config_not_ready")
    runtime, _pid = _tracked_process(service_id)
    if runtime == "running":
        raise HTTPException(status_code=409, detail="service_already_running")
    conflict = _mount_conflict(service_id, settings)
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"mount_owned_by:{conflict}",
        )
    if service_id == "rtai_supervisor":
        shared = service_status(
            "rtai_shared_ai",
            settings,
            include_health=True,
        )
        if shared["state"] != "healthy":
            raise HTTPException(
                status_code=409,
                detail="shared_ai_not_healthy",
            )
    command, cwd = _build_command(service_id, config)
    log_path = _runtime_dir() / f"{service_id}.log"
    environment = dict(os.environ)
    environment["RADIOTEDU_ONAIR_CONTROLLED"] = "1"
    log_file = log_path.open("ab", buffering=0)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        log_file.close()
        raise HTTPException(
            status_code=409,
            detail="service_launch_failed",
        ) from exc
    time.sleep(0.15)
    if proc.poll() is not None:
        log_file.close()
        raise HTTPException(status_code=409, detail="service_exited_on_start")
    details = _process_details(proc.pid)
    if not details:
        proc.terminate()
        log_file.close()
        raise HTTPException(status_code=409, detail="process_tracking_failed")
    with _LOCK:
        _PROCESSES[_process_registry_key(service_id)] = proc
        ledger = _load_ledger()
        ledger[service_id] = {
            "pid": int(proc.pid),
            "fingerprint": _process_fingerprint(details),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_ledger(ledger)
    log_file.close()
    return {"ok": True, "action": "start", "pid": int(proc.pid)}


def _stop(service_id: str) -> dict[str, Any]:
    with _LOCK:
        runtime, pid = _tracked_process(service_id)
        if runtime != "running" or not pid:
            raise HTTPException(status_code=409, detail="service_not_running")
        ledger = _load_ledger()
        record = ledger.get(service_id) or {}
        details = _process_details(pid)
        if not details or _process_fingerprint(details) != str(
            record.get("fingerprint") or ""
        ):
            raise HTTPException(
                status_code=409,
                detail="process_identity_changed",
            )
        process_key = _process_registry_key(service_id)
        proc = _PROCESSES.get(process_key)
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=8)
            except (OSError, SystemError, subprocess.TimeoutExpired):
                pass
        if _process_details(pid):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except (OSError, SystemError, subprocess.TimeoutExpired):
                # The OS-level identity check above is authoritative. Reaping a
                # stale Windows Popen handle must never block service cleanup.
                pass
        _PROCESSES.pop(process_key, None)
        ledger.pop(service_id, None)
        _save_ledger(ledger)
    return {"ok": True, "action": "stop"}


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum():
            values[key] = value.strip().strip('"').strip("'")
    return values


def _postgres_environment(values: dict[str, str]) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGSSLMODE",
    ):
        if values.get(key):
            environment[key] = values[key]
    database_url = values.get("DATABASE_URL", "")
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise HTTPException(status_code=409, detail="invalid_database_url")
        environment.update(
            {
                "PGHOST": parsed.hostname or "",
                "PGPORT": str(parsed.port or 5432),
                "PGDATABASE": unquote(parsed.path.lstrip("/")),
                "PGUSER": unquote(parsed.username or ""),
                "PGPASSWORD": unquote(parsed.password or ""),
            }
        )
        query = dict(
            item.split("=", 1)
            for item in parsed.query.split("&")
            if "=" in item
        )
        if query.get("sslmode"):
            environment["PGSSLMODE"] = query["sslmode"]
    if not environment.get("PGDATABASE"):
        raise HTTPException(
            status_code=409,
            detail="database_credentials_not_configured",
        )
    return environment


def _run_quiet(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
    failure: str,
) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=409, detail=failure) from exc
    if result.returncode != 0:
        raise HTTPException(status_code=409, detail=failure)


def _update_postgres_database(
    service_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    source = Path(config["source_dir"])
    env_file = Path(config["config_path"])
    backup_root = Path(config["database_backup_dir"])
    if not backup_root.is_absolute():
        raise HTTPException(
            status_code=409,
            detail="database_backup_dir_not_configured",
        )
    backup_root.mkdir(parents=True, exist_ok=True)
    values = _read_env(env_file)
    environment = _postgres_environment(values)
    configured_pg_dump = str(
        os.getenv("RADIOTEDU_PG_DUMP_BIN", "")
    ).strip()
    pg_dump = (
        configured_pg_dump
        if configured_pg_dump
        and Path(configured_pg_dump).is_absolute()
        and Path(configured_pg_dump).is_file()
        else shutil.which("pg_dump.exe") or shutil.which("pg_dump")
    )
    if not pg_dump:
        raise HTTPException(status_code=409, detail="pg_dump_not_found")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_root / f"{service_id}-{timestamp}.dump"
    _run_quiet(
        [pg_dump, "--format=custom", "--file", str(backup_path)],
        cwd=source,
        environment=environment,
        timeout=300,
        failure="database_backup_failed",
    )
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise HTTPException(status_code=409, detail="database_backup_failed")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise HTTPException(status_code=409, detail="npm_not_found")
    for migration in SERVICE_DEFINITIONS[service_id]["migrations"]:
        _run_quiet(
            [npm, "run", str(migration)],
            cwd=source,
            environment=environment,
            timeout=600,
            failure="database_migration_failed",
        )
    return {
        "ok": True,
        "action": "update_database",
        "backup_file": str(backup_path),
        "migrations_applied": len(
            SERVICE_DEFINITIONS[service_id]["migrations"]
        ),
    }


def _update_rtai_database(config: dict[str, Any]) -> dict[str, Any]:
    source = Path(config["source_dir"])
    config_root = Path(config["config_path"])
    backup_root = Path(config["database_backup_dir"])
    if not backup_root.is_absolute():
        raise HTTPException(
            status_code=409,
            detail="database_backup_dir_not_configured",
        )
    env_file = config_root / "RadioTEDU.BroadcastSupervisor.env"
    if not env_file.is_file():
        raise HTTPException(
            status_code=409,
            detail="supervisor_environment_not_found",
        )
    backup_root.mkdir(parents=True, exist_ok=True)
    old_cwd = Path.cwd()
    old_path = list(sys.path)
    modules_before = set(sys.modules)
    try:
        os.chdir(source)
        sys.path.insert(0, str(source))
        from backend.config import Settings
        from backend.database import init_db
        from backend.music_library import scan_music
        from backend.stations.context import build_station_context
        from backend.stations.loader import load_station_profiles

        base = Settings.from_env(env_file)
        profiles = load_station_profiles(base.station_profiles_path)
        results = []
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for station_id in ("radiotedu-en", "radiotedu-fr"):
            context = build_station_context(base, profiles[station_id])
            database = context.database_file
            backup = backup_root / f"{station_id}-{timestamp}.db"
            if database.is_file():
                shutil.copy2(database, backup)
            init_db(context)
            scan = scan_music(context)
            results.append(
                {
                    "station_id": station_id,
                    "backup_file": str(backup) if backup.exists() else "",
                    "tracks_found": int(scan.tracks_found),
                }
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail="ai_database_update_failed",
        ) from exc
    finally:
        os.chdir(old_cwd)
        sys.path[:] = old_path
        for module_name in set(sys.modules) - modules_before:
            if module_name == "backend" or module_name.startswith("backend."):
                sys.modules.pop(module_name, None)
    return {
        "ok": True,
        "action": "update_database",
        "stations": results,
    }


def _update_repository(
    service_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    if SERVICE_DEFINITIONS[service_id]["kind"] == "ollama":
        raise HTTPException(
            status_code=409,
            detail="repository_update_not_supported",
        )
    runtime, _pid = _tracked_process(service_id)
    if runtime == "running":
        raise HTTPException(
            status_code=409,
            detail="stop_service_before_repository_update",
        )
    source = _source_status(service_id, config)
    root = Path(str(config.get("source_dir") or ""))
    if not source["ready"] or not (root / ".git").exists():
        raise HTTPException(status_code=409, detail="service_repository_not_ready")
    if source["dirty"]:
        raise HTTPException(
            status_code=409,
            detail="repository_has_local_changes",
        )
    previous = str(source.get("commit") or "")
    commands = (
        ["git", "-C", str(root), "fetch", "--prune", "origin"],
        ["git", "-C", str(root), "merge", "--ff-only", "@{upstream}"],
    )
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    try:
        for command in commands:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=180,
                env=environment,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            status_code=409,
            detail="repository_fast_forward_failed",
        ) from exc
    updated = _source_status(service_id, config)
    return {
        "ok": True,
        "action": "update_repository",
        "previous_commit": previous,
        "commit": str(updated.get("commit") or ""),
        "changed": previous != str(updated.get("commit") or ""),
    }


def _pull_ollama_model(model_name: str) -> dict[str, Any]:
    model = str(model_name or "").strip()
    if not model or len(model) > 120 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
        model,
    ):
        raise HTTPException(status_code=400, detail="invalid_ollama_model")
    executable = _ollama_executable()
    if not executable:
        raise HTTPException(status_code=409, detail="ollama_not_installed")
    try:
        subprocess.run(
            [str(executable), "pull", model],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail="ollama_model_install_timed_out",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            status_code=409,
            detail="ollama_model_install_failed",
        ) from exc
    return {"ok": True, "action": "pull_model", "model": model}


def perform_action(
    service_id: str,
    action: str,
    confirmation: str,
    settings: dict[str, dict[str, Any]],
    model_name: str = "",
) -> dict[str, Any]:
    if service_id not in SERVICE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="unknown_service")
    action = str(action or "").strip().lower()
    if action == "check":
        return {
            "ok": True,
            "action": "check",
            "service": service_status(service_id, settings),
        }
    expected = CONFIRMATIONS.get(action)
    if not expected:
        raise HTTPException(status_code=400, detail="unknown_service_action")
    if str(confirmation or "") != expected:
        raise HTTPException(status_code=400, detail="confirmation_required")
    if action == "start":
        return _start(service_id, settings)
    if action == "stop":
        return _stop(service_id)
    if action == "restart":
        _stop(service_id)
        return _start(service_id, settings)
    if action == "update_repository":
        return _update_repository(service_id, settings[service_id])
    if action == "pull_model":
        if service_id != "ollama_runtime":
            raise HTTPException(
                status_code=409,
                detail="model_install_not_supported",
            )
        return _pull_ollama_model(model_name)
    definition = SERVICE_DEFINITIONS[service_id]
    if not definition["database_supported"]:
        raise HTTPException(
            status_code=409,
            detail="database_update_not_supported",
        )
    runtime, _pid = _tracked_process(service_id)
    if runtime == "running":
        raise HTTPException(
            status_code=409,
            detail="stop_service_before_database_update",
        )
    config = settings[service_id]
    source = _source_status(service_id, config)
    if not source["ready"]:
        raise HTTPException(status_code=409, detail="service_source_not_ready")
    if service_id == "rtai_supervisor":
        result = _update_rtai_database(config)
    else:
        result = _update_postgres_database(service_id, config)
    _record_database_maintenance(service_id, result)
    return result


def auto_start_enabled(settings: dict[str, dict[str, Any]]) -> list[str]:
    started: list[str] = []
    for service_id, config in settings.items():
        if not config.get("enabled") or not config.get("auto_start"):
            continue
        # These agents are deliberately supervised by Windows SCM once an
        # operator has completed a verified foreground enrollment.  The OnAir
        # backend may still launch them manually for foreground diagnostics,
        # but it must never become their autonomous boot supervisor.
        if _startup_owner(service_id) == STARTUP_OWNER_WINDOWS_SCM:
            continue
        try:
            _start(service_id, settings)
            started.append(service_id)
        except HTTPException:
            continue
    return started
