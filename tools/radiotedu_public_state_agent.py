"""Fail-closed local mirror of the deployed public status state.

This process is deliberately not an audio source, relay, or encoder.  It reads
the deployed PublicSyncService's *path contract* and atomically mirrors only
redacted public JSON plus database metadata for local health/reporting use.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit


CANONICAL_API_ORIGIN = "https://radiotedu.com"
DEFAULT_BACKEND_ROOT = Path(r"C:\RadioTEDU\RadioTEDU")
MAX_PUBLIC_JSON_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 1 * 1024 * 1024
SENSITIVE_NAME = re.compile(r"(?:pass(?:word)?|secret|token|api[_-]?key|credential|authorization|cookie)", re.I)
USERINFO_URL = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)


class SecurityViolation(RuntimeError):
    """A configuration, deployment source, or public-state boundary changed."""


@dataclass(frozen=True)
class AgentConfig:
    api_origin: str
    backend_root: Path
    backend_env_file: Path
    state_file: Path
    log_file: Path
    poll_seconds: float


@dataclass(frozen=True)
class SourceFingerprint:
    config: str
    environment: str
    public_sync: str


@dataclass
class AgentRuntime:
    config: AgentConfig
    paths: dict[str, Path]
    fingerprint: SourceFingerprint


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _windows_acl_is_protected(path: Path) -> bool:
    """Reject broadly writable protected inputs without interpreting their content."""
    try:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    unsafe_principals = (
        "everyone",
        "builtin\\\\users",
        "authenticated users",
        "anonymous logon",
        "codexsandboxusers",
    )
    for line in result.stdout.lower().splitlines():
        if any(principal in line for principal in unsafe_principals) and re.search(r"\((?:f|m|w|wdac|wo)\)", line):
            return False
    return True


def _protected_reference(value: object, label: str, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SecurityViolation(f"{label} must be a non-empty path reference")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise SecurityViolation(f"{label} must be an absolute normalized path reference")
    if must_exist:
        if not path.is_file() or path.is_symlink():
            raise SecurityViolation(f"{label} must reference an existing regular file")
        if os.name != "nt" and path.stat().st_mode & 0o002:
            raise SecurityViolation(f"{label} must not be world writable")
        if os.name == "nt" and not _windows_acl_is_protected(path):
            raise SecurityViolation(f"{label} must have a protected Windows ACL")
    return path


def _output_reference(value: object, label: str) -> Path:
    path = _protected_reference(value, label, must_exist=False)
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise SecurityViolation(f"{label} must reference a regular file")
    return path


def validate_api_origin(value: object) -> str:
    if not isinstance(value, str):
        raise SecurityViolation("api_origin must be https://radiotedu.com")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "radiotedu.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SecurityViolation("api_origin must be https://radiotedu.com")
    return CANONICAL_API_ORIGIN


def _load_strict_json(path: Path) -> Mapping[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityViolation("agent configuration must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise SecurityViolation("agent configuration must be a JSON object")
    return parsed


def load_agent_config(config_file: Path) -> AgentConfig:
    config_path = _protected_reference(str(config_file), "agent config")
    data = _load_strict_json(config_path)
    allowed = {
        "api_origin",
        "backend_root",
        "backend_env_file",
        "state_file",
        "log_file",
        "poll_seconds",
    }
    unexpected = set(data) - allowed
    if unexpected or any(SENSITIVE_NAME.search(str(key)) for key in data):
        raise SecurityViolation("agent configuration may contain references only; secret or unknown keys are forbidden")
    backend_root = Path(data.get("backend_root", str(DEFAULT_BACKEND_ROOT)))
    if not backend_root.is_absolute() or not backend_root.is_dir() or backend_root.is_symlink():
        raise SecurityViolation("backend_root must be an existing protected absolute directory")
    poll_seconds = data.get("poll_seconds", 5)
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 1 <= poll_seconds <= 300:
        raise SecurityViolation("poll_seconds must be between 1 and 300")
    return AgentConfig(
        api_origin=validate_api_origin(data.get("api_origin", CANONICAL_API_ORIGIN)),
        backend_root=backend_root,
        backend_env_file=_protected_reference(data.get("backend_env_file"), "backend_env_file"),
        state_file=_output_reference(data.get("state_file"), "state_file"),
        log_file=_output_reference(data.get("log_file"), "log_file"),
        poll_seconds=float(poll_seconds),
    )


@contextmanager
def _public_sync_env_reference(env_file: Path) -> Iterator[None]:
    key = "RADIOTEDU_PUBLIC_SYNC_ENV_FILE"
    before = os.environ.get(key)
    os.environ[key] = str(env_file)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before


def _load_public_sync_module(backend_root: Path) -> tuple[ModuleType, Path]:
    source = backend_root / "backend" / "public_sync.py"
    if not source.is_file() or source.is_symlink():
        raise SecurityViolation("deployed backend.public_sync source is unavailable")
    module_name = f"_radiotedu_public_sync_{_sha256_file(source)[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise SecurityViolation("deployed backend.public_sync cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise SecurityViolation("deployed backend.public_sync failed to load") from exc
    return module, source


def _build_public_sync_service(service_type: type[Any], env_file: Path) -> Any:
    factory = getattr(service_type, "from_env_file", None)
    if callable(factory):
        return factory(env_file)
    signature = inspect.signature(service_type)
    required = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if not required:
        return service_type()
    if len(required) == 1 and required[0].name in {"env_file", "environment_file", "env_path"}:
        return service_type(**{required[0].name: env_file})
    raise SecurityViolation("PublicSyncService must expose a no-argument or env-file path constructor")


def _configured_path_mapping(service: Any) -> Mapping[str, object]:
    for name in ("get_public_state_paths", "configured_public_state_paths", "public_state_paths"):
        candidate = getattr(service, name, None)
        value = candidate() if callable(candidate) else candidate
        if isinstance(value, Mapping):
            return value
    raise SecurityViolation("PublicSyncService does not expose its configured public-state paths")


def _normalize_public_state_paths(mapping: Mapping[str, object]) -> dict[str, Path]:
    aliases = {
        "en_status": ("en_status", "en_status_path", "status_en", "status_en_path"),
        "fr_status": ("fr_status", "fr_status_path", "status_fr", "status_fr_path"),
        "en_history": ("en_history", "en_history_path", "history_en", "history_en_path"),
        "fr_history": ("fr_history", "fr_history_path", "history_fr", "history_fr_path"),
        "database": ("database", "database_path", "db_path"),
    }
    normalized: dict[str, Path] = {}
    for target, names in aliases.items():
        value = next((mapping[name] for name in names if name in mapping), None)
        normalized[target] = _protected_reference(value, f"PublicSyncService {target} path")
    return normalized


def resolve_runtime(config_file: Path) -> AgentRuntime:
    config = load_agent_config(config_file)
    with _public_sync_env_reference(config.backend_env_file):
        module, source = _load_public_sync_module(config.backend_root)
        service_type = getattr(module, "PublicSyncService", None)
        if not isinstance(service_type, type):
            raise SecurityViolation("deployed backend.public_sync.PublicSyncService is unavailable")
        paths = _normalize_public_state_paths(_configured_path_mapping(_build_public_sync_service(service_type, config.backend_env_file)))
    return AgentRuntime(
        config=config,
        paths=paths,
        fingerprint=SourceFingerprint(
            config=_sha256_file(config_file),
            environment=_sha256_file(config.backend_env_file),
            public_sync=_sha256_file(source),
        ),
    )


def ensure_sources_unchanged(runtime: AgentRuntime, config_file: Path) -> None:
    actual = SourceFingerprint(
        config=_sha256_file(config_file),
        environment=_sha256_file(runtime.config.backend_env_file),
        public_sync=_sha256_file(runtime.config.backend_root / "backend" / "public_sync.py"),
    )
    if actual != runtime.fingerprint:
        raise SecurityViolation("configuration, environment reference, or PublicSyncService source changed")


def _redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_NAME.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return USERINFO_URL.sub(r"\1<redacted>@", value)
    return value


def _read_public_json(path: Path, label: str) -> Any:
    if path.stat().st_size > MAX_PUBLIC_JSON_BYTES:
        raise SecurityViolation(f"{label} exceeds the public-state size limit")
    try:
        return _redact(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityViolation(f"{label} is not valid public JSON") from exc


def _database_metadata(path: Path) -> dict[str, object]:
    metadata = path.stat()
    return {"bytes": metadata.st_size, "modified_at": datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat().replace("+00:00", "Z")}


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_redacted_log(path: Path, event: str, **details: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        backup = path.with_suffix(path.suffix + ".1")
        os.replace(path, backup)
    record = _redact({"at": _utc_now(), "event": event, **details})
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_once(runtime: AgentRuntime, config_file: Path) -> dict[str, object]:
    ensure_sources_unchanged(runtime, config_file)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "api_origin": runtime.config.api_origin,
        "generated_at": _utc_now(),
        "status": {
            "en": _read_public_json(runtime.paths["en_status"], "EN status"),
            "fr": _read_public_json(runtime.paths["fr_status"], "FR status"),
        },
        "history": {
            "en": _read_public_json(runtime.paths["en_history"], "EN history"),
            "fr": _read_public_json(runtime.paths["fr_history"], "FR history"),
        },
        "database": _database_metadata(runtime.paths["database"]),
    }
    ensure_sources_unchanged(runtime, config_file)
    _atomic_write_json(runtime.config.state_file, snapshot)
    _write_redacted_log(runtime.config.log_file, "state_written", database_bytes=snapshot["database"]["bytes"])
    return snapshot


def run_forever(runtime: AgentRuntime, config_file: Path) -> int:
    backoff = 1.0
    while True:
        try:
            run_once(runtime, config_file)
            backoff = 1.0
            time.sleep(runtime.config.poll_seconds)
        except KeyboardInterrupt:
            _write_redacted_log(runtime.config.log_file, "stopped")
            return 0
        except SecurityViolation:
            _write_redacted_log(runtime.config.log_file, "security_violation")
            return 2
        except Exception as exc:  # never log error text: it may include a protected path/value.
            _write_redacted_log(runtime.config.log_file, "retry", error_type=type(exc).__name__, retry_seconds=backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _config_reference_from_args(args: argparse.Namespace) -> Path:
    raw = args.config or os.environ.get("RADIOTEDU_PUBLIC_STATE_AGENT_CONFIG")
    if not raw:
        raise SecurityViolation("provide --config or RADIOTEDU_PUBLIC_STATE_AGENT_CONFIG as a protected config-file reference")
    return _protected_reference(raw, "agent config")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror deployed PublicSyncService state without network or playout operations.")
    parser.add_argument("--config", help="protected JSON config-file reference; no inline credentials are accepted")
    parser.add_argument("--check", action="store_true", help="preflight configuration/source validation only; writes no state or log")
    parser.add_argument("--once", action="store_true", help="write one atomic public-state snapshot then exit")
    args = parser.parse_args(argv)
    try:
        config_file = _config_reference_from_args(args)
        runtime = resolve_runtime(config_file)
        if args.check:
            print(json.dumps({"ok": True, "api_origin": runtime.config.api_origin, "paths": sorted(runtime.paths)}, sort_keys=True))
            return 0
        if args.once:
            run_once(runtime, config_file)
            return 0
        return run_forever(runtime, config_file)
    except SecurityViolation:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
