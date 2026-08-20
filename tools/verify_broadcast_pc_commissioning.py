"""Read-only, fail-closed Broadcast-PC commissioning verifier.

The verifier never starts, installs, restarts, or changes a service.  It writes
evidence only after every required Juke, Voting, AI, and public-state condition
has passed in one coherent probe cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


SENSITIVE_NAME = re.compile(r"(?:pass(?:word)?|secret|token|api[_-]?key|credential|authorization|cookie)", re.I)
UNSAFE_MOUNT_SETTING = re.compile(r"(?:icecast|ffmpeg|liquidsoap|source[_-]?(?:url|user|pass|credential)|mount[_-]?name)", re.I)
LOOPBACK = "127.0.0.1"
DECODER_SECONDS = 30


class CommissioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifierConfig:
    config_file: Path
    music_library_path: Path
    juke_health_url: str
    voting_health_url: str
    voting_audio_url: str
    public_ai_url: str
    event_url: str
    en_status_url: str
    fr_status_url: str
    ai_env_file: Path
    public_state_config_file: Path
    fingerprint_files: tuple[Path, ...]
    evidence_file: Path
    request_timeout_seconds: float
    decoder_path: str


class Probes(Protocol):
    def listeners(self) -> Mapping[int, set[str]]: ...

    def json(self, url: str, timeout_seconds: float) -> tuple[int, Any]: ...

    def status(self, url: str, timeout_seconds: float) -> int: ...

    def audio(self, url: str, timeout_seconds: float) -> tuple[int, Mapping[str, str], int]: ...

    def decode_public_audio(self, url: str, seconds: int, timeout_seconds: float) -> bool: ...


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _protected_file(value: object, label: str, *, exists: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CommissioningError(f"{label} must be an absolute file reference")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CommissioningError(f"{label} must be an absolute normalized file reference")
    if exists and (not path.is_file() or path.is_symlink()):
        raise CommissioningError(f"{label} must reference an existing regular file")
    if exists and os.name != "nt" and path.stat().st_mode & 0o002:
        raise CommissioningError(f"{label} must not be world writable")
    if not exists and path.exists() and (not path.is_file() or path.is_symlink()):
        raise CommissioningError(f"{label} must reference a regular file")
    return path


def _directory_reference(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CommissioningError(f"{label} must be an absolute directory reference")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise CommissioningError(f"{label} must be an absolute normalized directory reference")
    return path


def _decoder_reference(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommissioningError("decoder_path must be a command name or protected executable path")
    candidate = value.strip()
    if Path(candidate).is_absolute():
        executable = _protected_file(candidate, "decoder_path")
        if executable.suffix.lower() != ".exe":
            raise CommissioningError("decoder_path must be an executable path")
        return str(executable)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", candidate):
        raise CommissioningError("decoder_path must not contain command arguments")
    return candidate


def _safe_url(value: object, label: str, *, loopback: bool) -> str:
    if not isinstance(value, str):
        raise CommissioningError(f"{label} must be a URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise CommissioningError(f"{label} is not a safe URL")
    if loopback:
        if parsed.scheme != "http" or parsed.hostname != LOOPBACK:
            raise CommissioningError(f"{label} must be an HTTP {LOOPBACK} URL")
    elif parsed.scheme != "https":
        raise CommissioningError(f"{label} must use HTTPS")
    return value.strip()


def load_config(config_file: Path) -> VerifierConfig:
    config_path = _protected_file(str(config_file), "verifier config")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommissioningError("verifier config must be valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise CommissioningError("verifier config must be a JSON object")
    allowed = {
        "music_library_path", "juke_health_url", "voting_health_url", "voting_audio_url", "public_ai_url",
        "event_url", "en_status_url", "fr_status_url", "ai_env_file", "public_state_config_file",
        "fingerprint_files", "evidence_file", "request_timeout_seconds", "decoder_path",
    }
    if set(data) - allowed or any(SENSITIVE_NAME.search(str(key)) for key in data):
        raise CommissioningError("verifier config may contain only the documented non-secret fields")
    fingerprint_values = data.get("fingerprint_files")
    if not isinstance(fingerprint_values, list) or not fingerprint_values:
        raise CommissioningError("fingerprint_files must contain protected source/config references")
    timeout = data.get("request_timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise CommissioningError("request_timeout_seconds must be between 1 and 60")
    return VerifierConfig(
        config_file=config_path,
        music_library_path=_directory_reference(data.get("music_library_path"), "music_library_path"),
        juke_health_url=_safe_url(data.get("juke_health_url"), "juke_health_url", loopback=True),
        voting_health_url=_safe_url(data.get("voting_health_url"), "voting_health_url", loopback=True),
        voting_audio_url=_safe_url(data.get("voting_audio_url"), "voting_audio_url", loopback=True),
        public_ai_url=_safe_url(data.get("public_ai_url"), "public_ai_url", loopback=False),
        event_url=_safe_url(data.get("event_url"), "event_url", loopback=False),
        en_status_url=_safe_url(data.get("en_status_url"), "en_status_url", loopback=False),
        fr_status_url=_safe_url(data.get("fr_status_url"), "fr_status_url", loopback=False),
        ai_env_file=_protected_file(data.get("ai_env_file"), "ai_env_file"),
        public_state_config_file=_protected_file(data.get("public_state_config_file"), "public_state_config_file"),
        fingerprint_files=tuple(_protected_file(item, "fingerprint_files item") for item in fingerprint_values),
        evidence_file=_protected_file(data.get("evidence_file"), "evidence_file", exists=False),
        request_timeout_seconds=float(timeout),
        decoder_path=_decoder_reference(data.get("decoder_path", "ffmpeg")),
    )


def _flatten_truth(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value:
            return value[key]
    for child in value.values():
        nested = _flatten_truth(child, *keys)
        if nested is not None:
            return nested
    return None


def _required_true(value: Any, *keys: str) -> bool:
    return _flatten_truth(value, *keys) is True


def _required_false(value: Any, *keys: str) -> bool:
    return _flatten_truth(value, *keys) is False


def _is_2xx(value: Any) -> bool:
    return isinstance(value, int) and 200 <= value < 300


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint_set(paths: tuple[Path, ...]) -> dict[str, str] | None:
    try:
        return {f"path_{index}": _fingerprint(path) for index, path in enumerate(paths, start=1)}
    except OSError:
        return None


def _mountless_configuration(ai_env_file: Path, public_state_config_file: Path) -> bool:
    try:
        lines = ai_env_file.read_text(encoding="utf-8").splitlines()
        public_state = json.loads(public_state_config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    flags: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            return False
        key, value = stripped.split("=", 1)
        if UNSAFE_MOUNT_SETTING.search(key):
            return False
        flags[key.strip().upper()] = value.strip().lower()
    if not isinstance(public_state, Mapping) or any(UNSAFE_MOUNT_SETTING.search(str(key)) for key in public_state):
        return False
    return (
        flags.get("MOUNTLESS") == "true"
        or flags.get("PUBLIC_STATE_ONLY") == "true"
        or flags.get("AI_ICECAST_ENABLED") == "false"
        or flags.get("AI_STREAMING_ENABLED") == "false"
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SystemProbes:
    def __init__(self, decoder_path: str) -> None:
        self.decoder_path = decoder_path

    def listeners(self) -> Mapping[int, set[str]]:
        result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise CommissioningError("cannot enumerate TCP listeners")
        found: dict[int, set[str]] = {}
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) < 4 or columns[-2].upper() != "LISTENING":
                continue
            local = columns[1]
            host, separator, raw_port = local.rpartition(":")
            if not separator or not raw_port.isdigit():
                continue
            found.setdefault(int(raw_port), set()).add(host.strip("[]"))
        return found

    def json(self, url: str, timeout_seconds: float) -> tuple[int, Any]:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "RadioTEDU-CommissioningVerifier/1"})
        with urlopen(request, timeout=timeout_seconds) as response:  # no request body, auth, or write operation
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise CommissioningError("JSON response exceeds limit")
            return response.status, json.loads(raw.decode("utf-8"))

    def status(self, url: str, timeout_seconds: float) -> int:
        request = Request(url, headers={"User-Agent": "RadioTEDU-CommissioningVerifier/1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            response.read(256)
            return response.status

    def audio(self, url: str, timeout_seconds: float) -> tuple[int, Mapping[str, str], int]:
        request = Request(url, headers={"Icy-MetaData": "0", "User-Agent": "RadioTEDU-CommissioningVerifier/1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            sample = response.read(512)
            return response.status, dict(response.headers.items()), len(sample)

    def decode_public_audio(self, url: str, seconds: int, timeout_seconds: float) -> bool:
        started = time.monotonic()
        result = subprocess.run(
            [self.decoder_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(seconds), "-i", url, "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # A short successful decode is not evidence of a continuous stream.
        return result.returncode == 0 and time.monotonic() - started >= seconds - 1


def verify(config: VerifierConfig, probes: Probes) -> dict[str, Any]:
    fingerprint_paths = (config.config_file, config.ai_env_file, config.public_state_config_file, *config.fingerprint_files)
    fingerprint_before = _fingerprint_set(fingerprint_paths)
    if fingerprint_before is None:
        raise CommissioningError("a protected source/config fingerprint reference is unavailable")
    listeners = probes.listeners()
    juke_status, juke = probes.json(config.juke_health_url, config.request_timeout_seconds)
    voting_status, voting = probes.json(config.voting_health_url, config.request_timeout_seconds)
    audio_status, audio_headers, audio_bytes = probes.audio(config.voting_audio_url, config.request_timeout_seconds)
    event_status = probes.status(config.event_url, config.request_timeout_seconds)
    en_status, en_payload = probes.json(config.en_status_url, config.request_timeout_seconds)
    fr_status, fr_payload = probes.json(config.fr_status_url, config.request_timeout_seconds)
    public_decode_passed = probes.decode_public_audio(config.public_ai_url, DECODER_SECONDS, DECODER_SECONDS + config.request_timeout_seconds + 15)
    mountless_passed = _mountless_configuration(config.ai_env_file, config.public_state_config_file)
    checks = {
        # Field names align with installer/ProvisionBroadcastPcAgents.ps1.
        "operatorMusicLibraryPresent": config.music_library_path.is_dir(),
        "jukeForegroundPassed": _is_2xx(juke_status) and _required_true(juke, "foreground_passed", "foregroundPassed"),
        "jukeLoopback3210": listeners.get(3210) == {LOOPBACK},
        "jukeWssConnected": _required_true(juke, "wss_connected", "wssConnected"),
        "jukeHeartbeat2xx": _is_2xx(_flatten_truth(juke, "heartbeat_status", "heartbeatStatus")),
        "jukeReconnectPassed": _required_true(juke, "reconnect_passed", "reconnectPassed"),
        "votingForegroundPassed": _is_2xx(voting_status) and _required_true(voting, "foreground_passed", "foregroundPassed"),
        "votingLoopback4317": listeners.get(4317) == {LOOPBACK},
        "votingLoopback4320": listeners.get(4320) == {LOOPBACK},
        "votingWssAuthenticated": _required_true(voting, "wss_authenticated", "wssAuthenticated"),
        "votingReconnectPassed": _required_true(voting, "reconnect_passed", "reconnectPassed"),
        "votingIcecastConnected": _required_true(voting, "icecast_connected", "icecastConnected"),
        "radioTeduStatusEndpoints200": _is_2xx(en_status) and _is_2xx(fr_status) and isinstance(en_payload, (dict, list)) and isinstance(fr_payload, (dict, list)),
        "radioTeduEnEndpoint200": _is_2xx(en_status) and isinstance(en_payload, (dict, list)),
        "radioTeduFrEndpoint200": _is_2xx(fr_status) and isinstance(fr_payload, (dict, list)),
        "votingSoleAiSource": _flatten_truth(voting, "ai_source_owner", "aiSourceOwner") == "voting",
        # Extra conditions required for the final commissioning record.
        "jukeMirrorDisabled": _required_false(juke, "mirror_enabled", "mirrorEnabled", "ai_mirror_enabled", "aiMirrorEnabled"),
        "jukeAutoplayDisabled": _required_false(juke, "autoplay_enabled", "autoplayEnabled"),
        "votingLocalAudio": _is_2xx(audio_status) and audio_bytes > 0 and str(audio_headers.get("Content-Type", audio_headers.get("content-type", ""))).lower().startswith("audio/"),
        "publicAiContinuousDecodable30s": public_decode_passed,
        "publicAiDecode30Seconds": public_decode_passed,
        "eventStatus200": _is_2xx(event_status),
        "publicEventEndpointChecked": _is_2xx(event_status),
        "mountlessAiPublicStateConfig": mountless_passed,
        "aiPublicStateMountless": mountless_passed,
    }
    fingerprints = _fingerprint_set(fingerprint_paths)
    fingerprint_verified = fingerprints is not None and fingerprints == fingerprint_before
    checks["sourceConfigFingerprintsStable"] = fingerprint_verified
    checks["aiPublicStateSourceFingerprintVerified"] = fingerprint_verified
    if not all(checks.values()):
        failed = [name for name, value in checks.items() if not value]
        raise CommissioningError("required commissioning condition failed: " + ", ".join(failed))
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAtUtc": _now(),
        "checks": checks,
        "fingerprints": fingerprints,
    }
    _atomic_json(config.evidence_file, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only fail-closed Broadcast-PC commissioning verifier")
    parser.add_argument("--config", required=True, help="protected non-secret JSON config file")
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
        verify(config, SystemProbes(config.decoder_path))
        print(json.dumps({"ok": True, "evidence": str(config.evidence_file)}))
        return 0
    except (CommissioningError, OSError, subprocess.SubprocessError, socket.error):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
