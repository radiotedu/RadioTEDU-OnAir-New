from pathlib import Path

import app.config as config
import app.dependency_bootstrap as dependency_bootstrap


def test_frozen_runtime_uses_programdata_for_shared_state(monkeypatch, tmp_path):
    program_data = tmp_path / "ProgramData"
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("CLEANROOM_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLEANROOM_USER_CONFIG_ROOT", raising=False)

    assert config.get_data_root() == (
        program_data / "RadioTEDU" / "OnAir"
    ).resolve()
    assert config.get_user_config_root() == (
        local_app_data / "RadioTEDU" / "OnAir"
    ).resolve()


def test_source_runtime_honors_explicit_shared_data_root(monkeypatch, tmp_path):
    data_root = tmp_path / "isolated-data"
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))

    assert config.get_data_root() == data_root.resolve()


def test_frozen_runtime_keeps_binaries_beside_packaged_executable(
    monkeypatch, tmp_path
):
    packaged_backend = tmp_path / "Program Files" / "RadioTEDU" / "OnAir" / "backend.exe"
    monkeypatch.setattr(dependency_bootstrap.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        dependency_bootstrap.sys, "executable", str(packaged_backend), raising=False
    )
    monkeypatch.delenv("CLEANROOM_TOOLS_DIR", raising=False)

    assert dependency_bootstrap.managed_tools_dir() == (
        packaged_backend.parent / "tools"
    ).resolve()


def test_dependency_state_is_mutable_shared_state(monkeypatch, tmp_path):
    data_root = tmp_path / "shared"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)

    assert dependency_bootstrap.bootstrap_state_path() == (
        data_root / "state" / "dependency-bootstrap.json"
    ).resolve()
