import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from tools.provision_rtmd_integrations import (
    RAW_AI_AGENT,
    parse_env,
    persist_onair_settings,
    raw_file_section,
)


def test_raw_handoff_sections_stop_before_the_next_file():
    handoff = "\n".join(
        [
            "=" * 80,
            f"RAW FILE: {RAW_AI_AGENT}",
            "=" * 80,
            "API_ORIGIN=https://radiotedu.example",
            "POLL_SECONDS=2",
            "",
            "=" * 80,
            r"RAW FILE: C:\private\next.env",
            "=" * 80,
            "SECRET_VALUE=must-not-cross-the-boundary",
        ]
    )

    section = raw_file_section(handoff, RAW_AI_AGENT)

    assert parse_env(section) == {
        "API_ORIGIN": "https://radiotedu.example",
        "POLL_SECONDS": "2",
    }
    assert "must-not-cross-the-boundary" not in section


def test_persisted_service_cards_derive_real_agent_health_ports(tmp_path):
    program_data = tmp_path / "ProgramData" / "RadioTEDU"
    voting_agent = program_data / "voting" / "agent.env"
    voting_backend = program_data / "voting" / "backend.env"
    juke_agent = program_data / "juke" / "media-agent.env"
    juke_backend = program_data / "juke" / "backend.env"
    for path in (voting_agent, voting_backend, juke_agent, juke_backend):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    voting_agent.write_text("PORT=4317\n", encoding="utf-8")
    juke_agent.write_text("MEDIA_AGENT_PORT=3210\n", encoding="utf-8")
    args = SimpleNamespace(
        onair_db=tmp_path / "cleanroom.db",
        program_data=program_data,
        ai_repo=tmp_path / "rtai",
        voting_repo=tmp_path / "voting-repo",
        juke_repo=tmp_path / "juke-repo",
    )

    persist_onair_settings(
        args,
        program_data / "config",
        voting_agent,
        voting_backend,
        juke_agent,
        juke_backend,
    )

    connection = sqlite3.connect(args.onair_db)
    try:
        raw = connection.execute(
            "SELECT value FROM system_settings "
            "WHERE key='radiotedu_service_control_v1'"
        ).fetchone()[0]
    finally:
        connection.close()
    settings = json.loads(raw)
    assert settings["voting_agent"]["health_urls"] == [
        "http://127.0.0.1:4317/api/health"
    ]
    assert settings["juke_media_agent"]["health_urls"] == [
        "http://127.0.0.1:3210/v1/health"
    ]
    assert all(not item["auto_start"] for item in settings.values())
