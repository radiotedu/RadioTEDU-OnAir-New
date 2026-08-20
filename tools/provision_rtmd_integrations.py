"""Provision RadioTEDU integrations from the private machine handoff.

The handoff file is read locally and is never copied into the repository.  This
script writes only to the explicitly selected ProgramData directory and updates
the OnAir settings database with paths and health endpoints, never secret
values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import quote


SERVICE_SETTINGS_KEY = "radiotedu_service_control_v1"

RAW_AI_AGENT = r"C:\ProgramData\RadioTEDU\ai-broadcast-agent\config\agent.env"
RAW_WEB_HMAC = r"C:\ProgramData\RadioTEDU\secrets\web-hmac.env"
RAW_JUKE_BACKEND = r"C:\Users\tedu\Desktop\juke-local\backend\.env.example"
RAW_JUKE_AGENT = r"C:\Users\tedu\Desktop\juke-local\media-agent\.env"
RAW_VOTING_BACKEND = (
    r"C:\Users\tedu\Desktop\voting\rtjukebox\backend\.env.example"
)
RAW_VOTING_AGENT = (
    r"C:\Users\tedu\Desktop\voting\rtjukebox"
    r"\tools\local-voting-agent\.env"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rt-md", type=Path, required=True)
    parser.add_argument("--onair-db", type=Path, required=True)
    parser.add_argument("--ai-repo", type=Path, required=True)
    parser.add_argument("--voting-repo", type=Path, required=True)
    parser.add_argument("--juke-repo", type=Path, required=True)
    parser.add_argument(
        "--program-data",
        type=Path,
        default=Path(r"C:\ProgramData\RadioTEDU"),
    )
    parser.add_argument(
        "--music-library",
        type=Path,
        default=Path(r"E:\RadioTEDU Song Database\lofi"),
    )
    parser.add_argument(
        "--ffmpeg",
        type=Path,
        default=Path(
            r"C:\Users\tedu\AppData\Local\RadioTEDU Broadcast Wall"
            r"\tools\bin\ffmpeg.exe"
        ),
    )
    parser.add_argument(
        "--ffprobe",
        type=Path,
        default=Path(
            r"C:\Users\tedu\AppData\Local\RadioTEDU Broadcast Wall"
            r"\tools\bin\ffprobe.exe"
        ),
    )
    parser.add_argument(
        "--qwen-model",
        type=Path,
        default=Path(
            r"C:\Users\tedu\AppData\Local\Programs"
            r"\RadioTEDU Broadcast Wall\_internal\models"
            r"\qwen3-tts-voice-design"
        ),
    )
    return parser.parse_args()


def raw_file_section(handoff: str, raw_path: str) -> str:
    lines = handoff.splitlines()
    marker = f"RAW FILE: {raw_path}"
    try:
        marker_index = next(
            index for index, line in enumerate(lines) if line.strip() == marker
        )
    except StopIteration as exc:
        raise RuntimeError(f"required handoff section missing: {raw_path}") from exc

    start = marker_index + 1
    while start < len(lines) and (
        not lines[start].strip()
        or set(lines[start].strip()) == {"="}
    ):
        start += 1
    end = len(lines)
    for index in range(start, len(lines) - 1):
        if (
            lines[index].strip()
            and set(lines[index].strip()) == {"="}
            and lines[index + 1].strip().startswith("RAW FILE:")
        ):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def parse_env(text: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum():
            output[key] = value.strip()
    return output


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    username = os.environ.get("USERNAME", "").strip()
    grants = ["SYSTEM:(F)", "Administrators:(F)"]
    if username:
        grants.append(f"{username}:(F)")
    command = ["icacls", str(path), "/inheritance:r"]
    for grant in grants:
        command.extend(("/grant:r", grant))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to protect configuration file: {path}")


def persistent_secret(existing: dict[str, str], key: str) -> str:
    current = existing.get(key, "").strip()
    if current and "change-me" not in current.lower():
        return current
    return secrets.token_urlsafe(48)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_nonempty(values: dict[str, str], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if not values.get(key, "").strip()]
    if missing:
        raise RuntimeError(
            "required protected values are absent from rt.md: "
            + ", ".join(missing)
        )


def provision_ai(
    args: argparse.Namespace,
    handoff: str,
    voting_agent: dict[str, str],
) -> tuple[Path, Path]:
    config_root = args.program_data / "config"
    secret_root = args.program_data / "secrets"
    ai_state = args.program_data / "ai-broadcast-agent"
    runtime = args.program_data / "ai-radio"
    voice_root = runtime / "voices"
    warmup_path = config_root / "qwen-warmup.json"
    shared_path = config_root / "RadioTEDU.SharedAI.env"
    supervisor_path = config_root / "RadioTEDU.BroadcastSupervisor.env"

    hmac = parse_env(raw_file_section(handoff, RAW_WEB_HMAC))
    require_nonempty(
        hmac,
        (
            "RADIOTEDU_EN_SNAPSHOT_SECRET",
            "RADIOTEDU_FR_SNAPSHOT_SECRET",
        ),
    )
    write_private(secret_root / "web-hmac.env", render_env(hmac))

    agent = parse_env(raw_file_section(handoff, RAW_AI_AGENT))
    agent.update(
        {
            "SECRETS_FILE": str(secret_root / "web-hmac.env"),
            "STATE_FILE": str(ai_state / "state" / "state.json"),
            "LOG_FILE": str(ai_state / "logs" / "agent.log"),
            "EN_STATUS_FILE": str(runtime / "state" / "radiotedu-en.json"),
            "FR_STATUS_FILE": str(runtime / "state" / "radiotedu-fr.json"),
            "EN_HISTORY_FILE": str(runtime / "history" / "radiotedu-en.jsonl"),
            "FR_HISTORY_FILE": str(runtime / "history" / "radiotedu-fr.jsonl"),
            "EN_DATABASE_FILE": str(runtime / "db" / "radiotedu-en.sqlite3"),
            "FR_DATABASE_FILE": str(runtime / "db" / "radiotedu-fr.sqlite3"),
            "EN_STREAM_URL": "https://stream.radiotedu.com/ai",
            "FR_STREAM_URL": "https://stream.radiotedu.com/event",
            "EN_STREAM_MOUNT": "/ai",
            "FR_STREAM_MOUNT": "/event",
        }
    )
    write_private(
        ai_state / "config" / "agent.env",
        render_env(agent),
    )

    model_file = args.qwen_model / "model.safetensors"
    if not model_file.is_file():
        raise RuntimeError(f"Qwen model file not found: {model_file}")
    model_sha256 = sha256_file(model_file)
    voice_root.mkdir(parents=True, exist_ok=True)
    for directory in (
        runtime / "state",
        runtime / "history",
        runtime / "db",
        ai_state / "state",
        ai_state / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # The checked-in voice manifests are explicitly unapproved.  A structurally
    # valid warmup request is written, but no voice recording is fabricated.
    warmup = {
        "request_id": "radiotedu-local-warmup",
        "station_id": "radiotedu-en",
        "language": "en",
        "locale": "en-US",
        "normalized_text": "RadioTEDU local speech service warmup.",
        "announcement_label": "station_id",
        "voice": {
            "station_id": "radiotedu-en",
            "language": "en",
            "locale": "en-US",
            "voice_pack": "radiotedu-en-pending-approval",
            "host_id": "pending-host",
            "style_id": "warmup",
            "clone_prompt_path": str(voice_root / "pending-prompt.txt"),
            "reference_audio_path": str(voice_root / "pending-reference.wav"),
            "reference_transcript": "RadioTEDU local speech service warmup.",
            "model_checksum": f"sha256:{model_sha256}",
        },
        "finishing_policy_version": "radiotedu-wav-v1",
    }
    write_private(
        warmup_path,
        json.dumps(warmup, indent=2, ensure_ascii=False) + "\n",
    )

    icecast_password = (
        voting_agent.get("ICECAST_SOURCE_PASSWORD", "").strip()
        or voting_agent.get("AI_ICECAST_SOURCE_PASSWORD", "").strip()
    )
    if not icecast_password:
        raise RuntimeError("Icecast source password is absent from rt.md")

    shared = {
        "QWEN_TTS_HOST": "127.0.0.1",
        "QWEN_TTS_PORT": "8090",
        "QWEN_MODEL_ID": str(args.qwen_model),
        "QWEN_DEVICE": "cpu",
        "QWEN_DTYPE": "float16",
        "QWEN_MODEL_CHECKSUM_FILE": str(model_file),
        "QWEN_MODEL_SHA256": model_sha256,
        "QWEN_WARMUP_REQUEST_JSON": str(warmup_path),
        "QWEN_VOICE_ROOT": str(voice_root),
        "OLLAMA_COMMAND": "ollama",
    }
    supervisor = {
        "STATION_PROFILES_DIR": str(args.ai_repo / "config" / "stations"),
        "PLAYBACK_BACKEND": "liquidsoap",
        "AUTONOMY_ENABLED": "true",
        "QWEN_TTS_SERVICE_URL": "http://127.0.0.1:8090",
        "OLLAMA_URL": "http://127.0.0.1:11434",
        "LIQUIDSOAP_ENABLED": "true",
        "LIQUIDSOAP_COMMAND": "liquidsoap",
        "LIQUIDSOAP_HOST": "10.98.98.75",
        "LIQUIDSOAP_PORT": "11154",
        "LIQUIDSOAP_ENCODER_PROFILE": "aac_192",
        "LIQUIDSOAP_PUBLIC": "true",
        "PUBLIC_SYNC_URL": hmac.get(
            "RADIOTEDU_PLATFORM_API_URL",
            "https://api.radiotedu.com",
        ),
        "PUBLIC_SYNC_INTERVAL_SECONDS": "10",
        "PUBLIC_COMPATIBILITY_ENABLED": "false",
        "JINGLE_ENABLED": "true",
        "JINGLE_INTERVAL_TRACKS": "2",
        "IMAGING_RELEASE_ROOT": str(args.ai_repo),
        "RADIOTEDU_AGENT_ID": hmac.get(
            "RADIOTEDU_AGENT_ID",
            "school-radio-pc",
        ),
        "RADIOTEDU_AGENT_SCOPE": hmac.get(
            "RADIOTEDU_AGENT_SCOPE",
            "agent:playout",
        ),
        "RADIOTEDU_EN_SOURCE_CREDENTIALS": icecast_password,
        "RADIOTEDU_FR_SOURCE_CREDENTIALS": icecast_password,
        "RADIOTEDU_EN_SNAPSHOT_SECRET": hmac[
            "RADIOTEDU_EN_SNAPSHOT_SECRET"
        ],
        "RADIOTEDU_FR_SNAPSHOT_SECRET": hmac[
            "RADIOTEDU_FR_SNAPSHOT_SECRET"
        ],
    }
    write_private(shared_path, render_env(shared))
    write_private(supervisor_path, render_env(supervisor))
    return shared_path, supervisor_path


def provision_voting(
    args: argparse.Namespace,
    handoff: str,
) -> tuple[Path, Path, dict[str, str]]:
    root = args.program_data / "voting"
    agent_path = root / "agent.env"
    backend_path = root / "backend.env"
    agent = parse_env(raw_file_section(handoff, RAW_VOTING_AGENT))
    require_nonempty(
        agent,
        ("ICECAST_SOURCE_PASSWORD", "RADIO_AGENT_REQUEST_SECRET"),
    )
    agent.update(
        {
            "MUSIC_LIBRARY_DIR": str(args.music_library),
            "JINGLE_LIBRARY_DIR": str(args.music_library / "jingles"),
            "ALBUM_ART_CACHE_DIR": str(root / "album-art-cache"),
            "FFMPEG_PATH": str(args.ffmpeg),
            "FFPROBE_PATH": str(args.ffprobe),
            "ICECAST_SOURCE_URL": "http://10.98.98.75:11154/ai",
            "LOCAL_HTTP_STREAM_ENABLED": "false",
        }
    )
    write_private(agent_path, render_env(agent))

    example = parse_env(raw_file_section(handoff, RAW_VOTING_BACKEND))
    existing = read_env(backend_path)
    db_password = persistent_secret(existing, "DB_PASSWORD")
    backend = {
        **example,
        "PORT": "3001",
        "NODE_ENV": "production",
        "DB_USER": "radiotedu_voting",
        "DB_PASSWORD": db_password,
        "DB_NAME": "radiotedu_voting",
        "DATABASE_URL": (
            "postgresql://radiotedu_voting:"
            f"{quote(db_password, safe='')}@127.0.0.1:5432/radiotedu_voting"
        ),
        "JWT_SECRET": persistent_secret(existing, "JWT_SECRET"),
        "JWT_REFRESH_SECRET": persistent_secret(
            existing,
            "JWT_REFRESH_SECRET",
        ),
        "RADIO_AGENT_REQUEST_SECRET": agent["RADIO_AGENT_REQUEST_SECRET"],
        "RADIO_AGENT_ALLOWED_IDS": agent.get(
            "RADIO_AGENT_ID",
            "radiotedu-voting-local",
        ),
        "RADIO_STREAM_URL": "https://stream.radiotedu.com/ai",
        "VOTING_STREAM_URL": "https://stream.radiotedu.com/ai",
        "MUSIC_LIBRARY_DIR": str(args.music_library),
        "JINGLE_LIBRARY_DIR": str(args.music_library / "jingles"),
        "FFMPEG_PATH": str(args.ffmpeg),
        "FFPROBE_PATH": str(args.ffprobe),
    }
    write_private(backend_path, render_env(backend))
    return agent_path, backend_path, agent


def provision_juke(
    args: argparse.Namespace,
    handoff: str,
) -> tuple[Path, Path]:
    root = args.program_data / "juke"
    agent_path = root / "media-agent.env"
    backend_path = root / "backend.env"
    agent = parse_env(raw_file_section(handoff, RAW_JUKE_AGENT))
    require_nonempty(
        agent,
        ("MEDIA_AGENT_REQUEST_SECRET", "MEDIA_AGENT_HEARTBEAT_SECRET"),
    )
    agent.update(
        {
            "LOCAL_MUSIC_ROOT": str(args.music_library),
            "AI_ICECAST_URL": "http://10.98.98.75:11154/event",
            "AI_MIRROR_FFMPEG_PATH": str(args.ffmpeg),
        }
    )
    write_private(agent_path, render_env(agent))

    example = parse_env(raw_file_section(handoff, RAW_JUKE_BACKEND))
    existing = read_env(backend_path)
    db_password = persistent_secret(existing, "DB_PASSWORD")
    backend = {
        **example,
        "PORT": "3002",
        "NODE_ENV": "production",
        "DB_USER": "radiotedu_juke",
        "DB_PASSWORD": db_password,
        "DB_NAME": "radiotedu_juke",
        "DATABASE_URL": (
            "postgresql://radiotedu_juke:"
            f"{quote(db_password, safe='')}@127.0.0.1:5432/radiotedu_juke"
        ),
        "JWT_SECRET": persistent_secret(existing, "JWT_SECRET"),
        "JWT_REFRESH_SECRET": persistent_secret(
            existing,
            "JWT_REFRESH_SECRET",
        ),
        "LOCAL_PLAYBACK_SIGNING_SECRET": persistent_secret(
            existing,
            "LOCAL_PLAYBACK_SIGNING_SECRET",
        ),
        "MEDIA_AGENT_REQUEST_SECRET": agent["MEDIA_AGENT_REQUEST_SECRET"],
        "MEDIA_AGENT_HEARTBEAT_SECRET": agent[
            "MEDIA_AGENT_HEARTBEAT_SECRET"
        ],
        "MEDIA_AGENT_ALLOWED_IDS": agent.get(
            "MEDIA_AGENT_ID",
            "radiotedu-juke-local",
        ),
        "RADIO_STREAM_URL": "https://stream.radiotedu.com/event",
        "LOCAL_MUSIC_ROOT": str(args.music_library),
    }
    write_private(backend_path, render_env(backend))
    return agent_path, backend_path


def persist_onair_settings(
    args: argparse.Namespace,
    ai_config_root: Path,
    voting_agent: Path,
    voting_backend: Path,
    juke_agent: Path,
    juke_backend: Path,
) -> None:
    voting_agent_values = read_env(voting_agent)
    juke_agent_values = read_env(juke_agent)
    settings = {
        "rtai_shared_ai": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(args.ai_repo),
            "config_path": str(ai_config_root),
            "health_urls": ["http://127.0.0.1:8090/health"],
            "database_backup_dir": str(
                args.program_data / "backups" / "ai"
            ),
        },
        "rtai_supervisor": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(args.ai_repo),
            "config_path": str(ai_config_root),
            "health_urls": [
                "http://127.0.0.1:8765/health",
                "http://127.0.0.1:8766/health",
            ],
            "database_backup_dir": str(
                args.program_data / "backups" / "ai"
            ),
        },
        "voting_agent": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(
                args.voting_repo / "tools" / "local-voting-agent"
            ),
            "config_path": str(voting_agent),
            "health_urls": [
                "http://127.0.0.1:"
                f"{voting_agent_values.get('PORT', '4317')}/api/health"
            ],
            "database_backup_dir": str(
                args.program_data / "backups" / "voting"
            ),
        },
        "voting_backend": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(args.voting_repo / "backend"),
            "config_path": str(voting_backend),
            "health_urls": ["http://127.0.0.1:3001/health"],
            "database_backup_dir": str(
                args.program_data / "backups" / "voting"
            ),
        },
        "juke_media_agent": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(args.juke_repo / "media-agent"),
            "config_path": str(juke_agent),
            "health_urls": [
                "http://127.0.0.1:"
                f"{juke_agent_values.get('MEDIA_AGENT_PORT', '3210')}"
                "/v1/health"
            ],
            "database_backup_dir": str(
                args.program_data / "backups" / "juke"
            ),
        },
        "juke_backend": {
            "enabled": True,
            "auto_start": False,
            "source_dir": str(args.juke_repo / "backend"),
            "config_path": str(juke_backend),
            "health_urls": ["http://127.0.0.1:3002/health"],
            "database_backup_dir": str(
                args.program_data / "backups" / "juke"
            ),
        },
    }
    for service in settings.values():
        Path(service["database_backup_dir"]).mkdir(
            parents=True,
            exist_ok=True,
        )
    args.onair_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.onair_db, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE
            SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (
                SERVICE_SETTINGS_KEY,
                json.dumps(settings, separators=(",", ":"), sort_keys=True),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    for required in (
        args.rt_md,
        args.ai_repo,
        args.voting_repo,
        args.juke_repo,
        args.music_library,
        args.ffmpeg,
        args.ffprobe,
    ):
        if not required.exists():
            raise RuntimeError(f"required path not found: {required}")
    handoff = args.rt_md.read_text(encoding="utf-8")
    voting_agent_path, voting_backend_path, voting_agent = provision_voting(
        args,
        handoff,
    )
    juke_agent_path, juke_backend_path = provision_juke(args, handoff)
    shared_path, _supervisor_path = provision_ai(
        args,
        handoff,
        voting_agent,
    )
    persist_onair_settings(
        args,
        shared_path.parent,
        voting_agent_path,
        voting_backend_path,
        juke_agent_path,
        juke_backend_path,
    )
    print("Provisioned 6 RadioTEDU service controls and 8 protected files.")
    print("Autostart remains disabled for every service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
