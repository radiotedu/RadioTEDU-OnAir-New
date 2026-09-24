from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from huggingface_hub import snapshot_download

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository

DEFAULT_LLM_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_OMNIVOICE_MODEL = "k2-fsa/OmniVoice"
DEFAULT_PROMPT_TEMPLATE = (
    "You're listening to {station_name}. "
    "Up next is {track_title}{artist_phrase}.{history_line}{educational_line}"
)


def _download_models(skip_models: bool) -> dict[str, str]:
    if skip_models:
        return {"llm_model": DEFAULT_LLM_MODEL, "omnivoice_model": DEFAULT_OMNIVOICE_MODEL}

    snapshot_download(
        repo_id=DEFAULT_LLM_MODEL,
        local_dir=None,
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    snapshot_download(
        repo_id=DEFAULT_OMNIVOICE_MODEL,
        local_dir=None,
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    return {"llm_model": DEFAULT_LLM_MODEL, "omnivoice_model": DEFAULT_OMNIVOICE_MODEL}


def _bootstrap_database() -> dict[str, str]:
    init_db()
    conn = get_connection()
    try:
        settings_repo = SettingsRepository(conn)
        cur = conn.cursor()
        cur.execute("UPDATE stations SET name=? WHERE id=1", ("Radio TED U Classical",))
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            ("display_brand_name", "Radio TED U Classical"),
        )
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            ("active_station_id", "1"),
        )
        cur.execute(
            "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            ("speaker_monitor_station_id", "1"),
        )
        conn.commit()

        settings_repo.upsert_station(
            1,
            {
                "ai_host_enabled": "true",
                "ai_llm_model": DEFAULT_LLM_MODEL,
                "ai_omnivoice_model": DEFAULT_OMNIVOICE_MODEL,
                "ai_tts_provider": "omnivoice",
                "ai_tts_model_path": "",
                "ai_voice_persona": "auto",
                "ai_announcement_max_seconds": "15",
                "ai_include_music_history": "true",
                "ai_educational_segments": "false",
                "ai_station_id_interval": "0",
                "ai_prompt_template": DEFAULT_PROMPT_TEMPLATE,
                "program_queue_source": "automation",
                "sweeper_enabled": "false",
                "startup_sound_enabled": "false",
            },
        )

        station = conn.execute("SELECT id, name FROM stations WHERE id=1").fetchone()
        return {
            "station_id": str(station["id"]),
            "station_name": str(station["name"]),
            "db_path": str(Path(os.getenv("CLEANROOM_DB_PATH", "")).expanduser().resolve())
            if os.getenv("CLEANROOM_DB_PATH", "").strip()
            else "",
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-model-downloads",
        action="store_true",
        help="Only seed the database/settings without downloading Hugging Face models.",
    )
    args = parser.parse_args()

    model_summary = _download_models(skip_models=bool(args.skip_model_downloads))
    db_summary = _bootstrap_database()
    print(json.dumps({"ok": True, **model_summary, **db_summary}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
