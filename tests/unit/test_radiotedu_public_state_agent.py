import json
import os
from pathlib import Path

import pytest

from tools import radiotedu_public_state_agent as agent


@pytest.fixture(autouse=True)
def _allow_test_filesystem_references(monkeypatch):
    monkeypatch.setattr(agent, "_windows_acl_is_protected", lambda _path: True)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _deployment(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "RadioTEDU"
    backend = root / "backend"
    backend.mkdir(parents=True)
    env_file = root / "public-sync.env"
    env_file.write_text("PUBLIC_SYNC_CONFIG=protected-reference\n", encoding="utf-8")
    state_paths = {
        "en_status": tmp_path / "state" / "en-status.json",
        "fr_status": tmp_path / "state" / "fr-status.json",
        "en_history": tmp_path / "state" / "en-history.json",
        "fr_history": tmp_path / "state" / "fr-history.json",
        "database": tmp_path / "state" / "public.sqlite",
    }
    for key, path in state_paths.items():
        if key == "database":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"SQLite format 3\\x00")
        else:
            _write(path, {"language": key[:2], "token": "must-not-escape", "stream_url": "https://user:pass@example.test/live"})
    source = """
from pathlib import Path
class PublicSyncService:
    def __init__(self, env_file):
        self.env_file = env_file
    def get_public_state_paths(self):
        return {mapping!r}
""".replace("{mapping!r}", repr({key: str(path) for key, path in state_paths.items()}))
    (backend / "public_sync.py").write_text(source, encoding="utf-8")
    config = tmp_path / "agent.json"
    _write(config, {
        "api_origin": "https://radiotedu.com",
        "backend_root": str(root),
        "backend_env_file": str(env_file),
        "state_file": str(tmp_path / "output" / "public-state.json"),
        "log_file": str(tmp_path / "output" / "public-state.log"),
        "poll_seconds": 1,
    })
    return root, env_file, config


def test_validate_api_origin_accepts_only_canonical_origin():
    assert agent.validate_api_origin("https://radiotedu.com") == "https://radiotedu.com"
    for invalid in ("http://radiotedu.com", "https://radiotedu.com:443", "https://evil.test", "https://user:pass@radiotedu.com"):
        with pytest.raises(agent.SecurityViolation):
            agent.validate_api_origin(invalid)


def test_resolve_runtime_uses_deployed_public_sync_path_contract(tmp_path):
    _root, _env_file, config = _deployment(tmp_path)
    runtime = agent.resolve_runtime(config)
    assert sorted(runtime.paths) == ["database", "en_history", "en_status", "fr_history", "fr_status"]
    assert runtime.config.api_origin == "https://radiotedu.com"


def test_run_once_writes_atomic_secret_free_state_and_redacted_log(tmp_path):
    _root, _env_file, config = _deployment(tmp_path)
    runtime = agent.resolve_runtime(config)
    snapshot = agent.run_once(runtime, config)
    state_text = runtime.config.state_file.read_text(encoding="utf-8")
    log_text = runtime.config.log_file.read_text(encoding="utf-8")
    assert snapshot["database"]["bytes"] > 0
    assert "must-not-escape" not in state_text
    assert "pass@" not in state_text
    assert "<redacted>" in state_text
    assert "state_written" in log_text


def test_source_or_config_change_fails_closed_before_new_state_write(tmp_path):
    root, _env_file, config = _deployment(tmp_path)
    runtime = agent.resolve_runtime(config)
    (root / "backend" / "public_sync.py").write_text("changed", encoding="utf-8")
    with pytest.raises(agent.SecurityViolation):
        agent.run_once(runtime, config)
    assert not runtime.config.state_file.exists()


def test_secret_keys_are_rejected_from_agent_config(tmp_path):
    _root, _env_file, config = _deployment(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["source_password"] = "not-accepted"
    config.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(agent.SecurityViolation):
        agent.load_agent_config(config)


def test_check_mode_writes_no_state_or_log(tmp_path, capsys):
    _root, _env_file, config = _deployment(tmp_path)
    assert agent.main(["--config", str(config), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert not (tmp_path / "output" / "public-state.json").exists()
    assert not (tmp_path / "output" / "public-state.log").exists()


def test_agent_contains_no_playout_or_source_client():
    source = Path(agent.__file__).read_text(encoding="utf-8").lower()
    assert "ffmpeg" not in source
    assert "liquidsoap" not in source
    assert "icecast" not in source
