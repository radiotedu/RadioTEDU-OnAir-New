import base64
import hashlib
import hmac
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.api.integrations as integrations
import app.services.radiotedu_service_control as control
from app.api.integrations import (
    RadioTEDUServiceAction,
    RadioTEDUServiceSettingsUpdate,
)


def _settings(tmp_path: Path):
    values = control.default_settings()
    return values


def test_settings_allow_only_fixed_services_safe_health_and_absolute_paths(
    tmp_path,
):
    values = _settings(tmp_path)
    assert values["voting_backend"]["health_urls"] == [
        "https://radiotedu.com/jukebox/api/v1/next-song-voting/status",
        "https://radiotedu.com/jukebox/api/v1/next-song-voting/rounds/active",
    ]
    assert values["juke_backend"]["health_urls"] == [
        "https://radiotedu.com/juke-local"
    ]
    values["juke_media_agent"].update(
        {
            "enabled": True,
            "source_dir": str(tmp_path / "agent"),
            "config_path": str(tmp_path / "agent.env"),
            "health_urls": ["http://127.0.0.1:3210/v1/health"],
            "command": "this field must never be accepted",
        }
    )

    normalized = control.normalize_settings(values)

    assert "command" not in normalized["juke_media_agent"]
    assert normalized["juke_media_agent"]["source_dir"] == str(
        (tmp_path / "agent").resolve()
    )
    values["juke_media_agent"]["health_urls"] = [
        "http://radio.example/health"
    ]
    with pytest.raises(HTTPException) as exc:
        control.normalize_settings(values)
    assert exc.value.detail == "non_loopback_health_url_requires_https"


def test_mutations_require_exact_confirmation_and_genre_voting_owns_no_mount(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    settings["voting_agent"]["enabled"] = True

    with pytest.raises(HTTPException) as exc:
        control.perform_action(
            "voting_agent",
            "start",
            "yes",
            settings,
        )
    assert exc.value.detail == "confirmation_required"

    monkeypatch.setattr(
        control,
        "_tracked_process",
        lambda service_id: (
            ("running", 99)
            if service_id == "rtai_supervisor"
            else ("stopped", None)
        ),
    )
    assert control._mount_conflict("voting_agent", settings) is None


def test_service_settings_api_persists_only_non_secret_control_fields(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    values = _settings(tmp_path)
    values["voting_backend"].update(
        {
            "source_dir": str(tmp_path / "voting-backend"),
            "config_path": str(tmp_path / "voting-backend.env"),
            "database_backup_dir": str(tmp_path / "backups"),
        }
    )

    result = integrations.update_radiotedu_services(
        RadioTEDUServiceSettingsUpdate(services=values),
        _user={},
    )
    loaded = integrations.get_radiotedu_services(
        refresh_health=False,
        _user={},
    )

    assert result["ok"] is True
    assert loaded["services"]["voting_backend"]["config_path"].endswith(
        "voting-backend.env"
    )
    assert "token" not in str(loaded).lower()
    assert "password" not in str(loaded).lower()


def test_database_action_is_never_available_for_agents(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(HTTPException) as exc:
        control.perform_action(
            "juke_media_agent",
            "update_database",
            "UPDATE DATABASE",
            settings,
        )
    assert exc.value.detail == "database_update_not_supported"


def test_ollama_is_first_class_but_optional(tmp_path, monkeypatch):
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixed-test-runtime")
    monkeypatch.setattr(control, "_ollama_executable", lambda: executable)
    settings = _settings(tmp_path)
    settings["ollama_runtime"]["enabled"] = True

    status = control.service_status(
        "ollama_runtime",
        settings,
        include_health=False,
    )

    assert status["source"]["ready"] is True
    assert status["config_ready"] is True
    assert status["state"] == "ready"
    assert control.SERVICE_DEFINITIONS["ollama_runtime"]["kind"] == "ollama"


def test_ollama_model_install_is_fixed_and_validated(tmp_path, monkeypatch):
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixed-test-runtime")
    calls = []
    monkeypatch.setattr(control, "_ollama_executable", lambda: executable)
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )
    settings = _settings(tmp_path)

    result = control.perform_action(
        "ollama_runtime",
        "pull_model",
        "INSTALL MODEL",
        settings,
        "qwen2.5:0.5b",
    )

    assert result == {
        "ok": True,
        "action": "pull_model",
        "model": "qwen2.5:0.5b",
    }
    assert calls == [[str(executable), "pull", "qwen2.5:0.5b"]]
    with pytest.raises(HTTPException) as exc:
        control.perform_action(
            "ollama_runtime",
            "pull_model",
            "INSTALL MODEL",
            settings,
            "qwen2.5:0.5b; remove-everything",
        )
    assert exc.value.detail == "invalid_ollama_model"


def test_repository_update_requires_clean_fast_forward(tmp_path, monkeypatch):
    source = tmp_path / "repository"
    (source / ".git").mkdir(parents=True)
    settings = _settings(tmp_path)
    settings["voting_backend"]["source_dir"] = str(source)
    commits = iter(("before", "after"))
    monkeypatch.setattr(
        control,
        "_source_status",
        lambda service_id, config: {
            "configured": True,
            "ready": True,
            "commit": next(commits),
            "dirty": False,
            "missing": [],
        },
    )
    monkeypatch.setattr(
        control,
        "_tracked_process",
        lambda service_id: ("stopped", None),
    )
    calls = []
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    result = control.perform_action(
        "voting_backend",
        "update_repository",
        "UPDATE REPOSITORY",
        settings,
    )

    assert result["changed"] is True
    assert result["commit"] == "after"
    assert [command[3] for command in calls] == ["fetch", "merge"]


def test_postgres_database_update_creates_backup_and_persists_health_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(tmp_path / "onair-data"))
    source = tmp_path / "voting-backend"
    (source / "dist").mkdir(parents=True)
    (source / "dist" / "server.js").write_text("", encoding="utf-8")
    (source / "package.json").write_text("{}", encoding="utf-8")
    env_file = tmp_path / "voting.env"
    env_file.write_text(
        "DATABASE_URL=postgres://radio:private@127.0.0.1:5432/radiotedu\n",
        encoding="utf-8",
    )
    backup_dir = tmp_path / "backups"
    settings = _settings(tmp_path)
    settings["voting_backend"].update(
        {
            "enabled": True,
            "source_dir": str(source),
            "config_path": str(env_file),
            "health_urls": [],
            "database_backup_dir": str(backup_dir),
        }
    )
    settings = control.normalize_settings(settings)
    commands = []

    monkeypatch.setattr(
        control.shutil,
        "which",
        lambda name: str(tmp_path / name),
    )
    monkeypatch.setattr(
        control,
        "_tracked_process",
        lambda _service_id: ("stopped", None),
    )

    def fake_run_quiet(command, **_kwargs):
        commands.append(command)
        if "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"backup")

    monkeypatch.setattr(control, "_run_quiet", fake_run_quiet)

    result = control.perform_action(
        "voting_backend",
        "update_database",
        "UPDATE DATABASE",
        settings,
    )
    status = control.service_status(
        "voting_backend",
        settings,
        include_health=False,
    )

    assert result["ok"] is True
    assert result["migrations_applied"] == 2
    assert Path(result["backup_file"]).read_bytes() == b"backup"
    assert [command[-2:] for command in commands[1:]] == [
        ["run", "db:migrate"],
        ["run", "db:migrate:voting-agent"],
    ]
    assert status["database"]["state"] == "updated"
    assert status["database"]["kind"] == "PostgreSQL"
    assert status["database"]["migrations_applied"] == 2
    assert status["database"]["last_backup_files"] == [result["backup_file"]]
    assert "private" not in str(status)


def test_juke_ai_mirror_participates_in_mount_ownership(tmp_path):
    env_file = tmp_path / "juke-agent.env"
    env_file.write_text("AI_MIRROR_ENABLED=true\n", encoding="utf-8")
    settings = _settings(tmp_path)
    settings["juke_media_agent"]["config_path"] = str(env_file)

    assert control._service_mounts(
        "juke_media_agent",
        settings["juke_media_agent"],
    ) == ["/ai"]


def test_disabled_services_do_not_delay_check_all(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        control,
        "_health_check",
        lambda _url: pytest.fail("disabled health URL was requested"),
    )

    status = control.service_status(
        "rtai_shared_ai",
        settings,
        include_health=True,
    )

    assert status["state"] == "disabled"
    assert status["health"] == []


def test_juke_health_headers_are_signed_without_exposing_the_secret(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "media-agent.env"
    config_path.write_text(
        "MEDIA_AGENT_REQUEST_SECRET=local-test-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(control.time, "time", lambda: 1_700_000_000)

    headers = control._juke_health_headers(
        "http://127.0.0.1:3210/v1/health?brief=1",
        {"config_path": str(config_path)},
    )

    message = b"GET\n/v1/health?brief=1\n1700000000\n"
    expected = base64.urlsafe_b64encode(
        hmac.new(
            b"local-test-secret",
            message,
            hashlib.sha256,
        ).digest()
    ).decode().rstrip("=")
    assert headers == {
        "X-Juke-Timestamp": "1700000000",
        "X-Juke-Signature": expected,
    }
    assert "local-test-secret" not in str(headers)


def test_rtai_command_prefers_repository_virtualenv_python(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "rtai"
    repository_python = source / ".venv" / "Scripts" / "python.exe"
    repository_python.parent.mkdir(parents=True)
    repository_python.write_bytes(b"")
    config_root = tmp_path / "config"
    config_root.mkdir()
    monkeypatch.setattr(
        control.shutil,
        "which",
        lambda command: (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            if command == "powershell.exe"
            else None
        ),
    )
    settings = _settings(tmp_path)
    settings["rtai_shared_ai"].update(
        {
            "source_dir": str(source),
            "config_path": str(config_root),
        }
    )

    command, working_directory = control._build_command(
        "rtai_shared_ai",
        settings["rtai_shared_ai"],
    )

    assert command[command.index("-Python") + 1] == str(repository_python)
    assert working_directory == source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_fixed_node_service_can_be_started_and_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(tmp_path / "onair-data"))
    source = tmp_path / "agent"
    source.mkdir()
    (source / "package.json").write_text(
        '{"name":"radiotedu-control-test","private":true}',
        encoding="utf-8",
    )
    (source / "server.js").write_text(
        "setInterval(() => {}, 1000);",
        encoding="utf-8",
    )
    env_file = tmp_path / "agent.env"
    env_file.write_text("TEST_MODE=true\n", encoding="utf-8")
    settings = _settings(tmp_path)
    settings["juke_media_agent"].update(
        {
            "enabled": True,
            "source_dir": str(source),
            "config_path": str(env_file),
            "health_urls": [],
        }
    )
    settings = control.normalize_settings(settings)

    started = control.perform_action(
        "juke_media_agent",
        "start",
        "START SERVICE",
        settings,
    )
    try:
        assert started["ok"] is True
        assert control.service_status(
            "juke_media_agent",
            settings,
            include_health=False,
        )["runtime"] == "running"
    finally:
        stopped = control.perform_action(
            "juke_media_agent",
            "stop",
            "STOP SERVICE",
            settings,
        )
    assert stopped["ok"] is True


def test_in_memory_process_registry_is_scoped_to_data_root(tmp_path, monkeypatch):
    class RunningProcess:
        pid = 43210

        @staticmethod
        def poll():
            return None

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(first_root / "cleanroom.db"))
    first_key = control._process_registry_key("juke_media_agent")
    control._PROCESSES[first_key] = RunningProcess()
    try:
        monkeypatch.setenv("CLEANROOM_DB_PATH", str(second_root / "cleanroom.db"))
        assert control._tracked_process("juke_media_agent") == ("stopped", None)
    finally:
        control._PROCESSES.pop(first_key, None)


def test_api_action_uses_backend_confirmation_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    with pytest.raises(HTTPException) as exc:
        integrations.control_radiotedu_service(
            "voting_agent",
            RadioTEDUServiceAction(action="start", confirmation=""),
            _user={},
        )
    assert exc.value.detail == "confirmation_required"


def _windows_scm_agent_settings(tmp_path: Path, service_id: str):
    source = tmp_path / service_id
    source.mkdir()
    (source / "package.json").write_text("{}", encoding="utf-8")
    if service_id == "juke_media_agent":
        (source / "server.js").write_text("setInterval(() => {}, 1000);", encoding="utf-8")
        env_text = "\n".join(
            [
                "MEDIA_AGENT_BIND_HOST=127.0.0.1",
                "AI_MIRROR_ENABLED=false",
                "AI_AUTOPLAY_ENABLED=false",
                "MEDIA_AGENT_REQUEST_SECRET=test-only-value",
            ]
        )
    else:
        entry = source / "scripts" / "voting-supervisor.mjs"
        entry.parent.mkdir()
        entry.write_text("setInterval(() => {}, 1000);", encoding="utf-8")
        env_text = "\n".join(
            [
                "PORT=4317",
                "LOCAL_HTTP_STREAM_ENABLED=true",
                "LOCAL_HTTP_STREAM_PORT=4320",
                "RADIO_AGENT_REQUEST_SECRET=test-only-value",
            ]
        )
    env_file = tmp_path / f"{service_id}.env"
    env_file.write_text(env_text + "\n", encoding="utf-8")
    settings = _settings(tmp_path)
    settings[service_id].update(
        {
            "enabled": True,
            "auto_start": True,
            "source_dir": str(source),
            "config_path": str(env_file),
            "health_urls": [],
        }
    )
    return control.normalize_settings(settings), env_file


def test_windows_scm_agents_are_never_backend_autostarted(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    for service_id in control.SCM_OWNED_SERVICE_IDS:
        settings[service_id].update({"enabled": True, "auto_start": True})
    calls = []
    monkeypatch.setattr(control, "_start", lambda service_id, _settings: calls.append(service_id))

    assert control.auto_start_enabled(settings) == []
    assert calls == []


def test_juke_foreground_verification_is_secret_free_and_invalidates_on_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "CLEANROOM_DB_PATH", str(tmp_path / "onair-data" / "cleanroom.db")
    )
    settings, env_file = _windows_scm_agent_settings(tmp_path, "juke_media_agent")
    config = settings["juke_media_agent"]

    initial = control.autonomous_startup_status("juke_media_agent", config)
    assert initial["owner"] == "windows_scm"
    assert initial["state"] == "verification_required"

    verified = control.record_successful_foreground_evidence(
        "juke_media_agent",
        config,
        {
            "loopback_ports": [3210],
            "mirror_disabled": True,
            "autoplay_disabled": True,
            "untrusted_note": "must-not-be-persisted",
        },
    )
    assert verified["ready"] is True
    assert verified["state"] == "verified"
    assert verified["evidence"] == {
        "loopback_ports": [3210],
        "mirror_disabled": True,
        "autoplay_disabled": True,
    }
    ledger_text = (tmp_path / "onair-data" / "radiotedu-services" / "foreground-verifications.json").read_text(encoding="utf-8")
    assert "test-only-value" not in ledger_text
    assert "untrusted_note" not in ledger_text

    env_file.write_text(env_file.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    stale = control.autonomous_startup_status("juke_media_agent", config)
    assert stale["ready"] is False
    assert stale["state"] == "verification_stale"


def test_voting_verification_requires_loopback_ports_and_sole_ai_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "CLEANROOM_DB_PATH", str(tmp_path / "onair-data" / "cleanroom.db")
    )
    settings, _env_file = _windows_scm_agent_settings(tmp_path, "voting_agent")
    config = settings["voting_agent"]

    with pytest.raises(HTTPException) as exc:
        control.record_successful_foreground_evidence(
            "voting_agent",
            config,
            {"loopback_ports": [4317, 4320], "ai_mount_owner": "other"},
        )
    assert exc.value.detail == "foreground_verification_failed"

    verified = control.record_successful_foreground_evidence(
        "voting_agent",
        config,
        {"loopback_ports": [4317, 4320], "ai_mount_owner": "voting_agent"},
    )
    assert verified["ready"] is True
    assert verified["evidence"] == {
        "loopback_ports": [4317, 4320],
        "ai_mount_owner": "voting_agent",
    }
    public = control.public_settings(settings)
    assert public["autonomous_startup"]["voting_agent"]["state"] == "verified"
    definition = next(item for item in public["definitions"] if item["id"] == "voting_agent")
    assert definition["startup_owner"] == "windows_scm"
    assert "test-only-value" not in str(public)
