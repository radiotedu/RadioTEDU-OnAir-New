from __future__ import annotations

from pathlib import Path

import pytest

from tools.run_radio_backend_service import configure_environment


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "RadioTEDU-OnAir-Radio"
    (root / "run" / "new-program" / "data").mkdir(parents=True)
    (root / "run" / "new-program" / "user" / "secrets").mkdir(parents=True)
    tools = root / "dist" / "backend-test" / "tools" / "bin"
    tools.mkdir(parents=True)
    (root / "run" / "new-program" / "data" / "cleanroom.db").write_bytes(b"db")
    (root / "run" / "new-program" / "user" / "secrets" / "jwt-signing.key").write_text(
        "test-only", encoding="utf-8"
    )
    (root / "run_cleanroom.py").write_text("def main(): pass\n", encoding="utf-8")
    (tools / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (root / "last_build_path.txt").write_text(
        str(root / "dist" / "backend-test" / "RadioTEDU-OnAir-Backend.exe"),
        encoding="utf-8",
    )
    return root


def _program_data(tmp_path: Path) -> Path:
    data_root = tmp_path / "ProgramData" / "RadioTEDU" / "OnAir"
    (data_root / "secrets").mkdir(parents=True)
    (data_root / "cleanroom.db").write_bytes(b"db")
    (data_root / "secrets" / "jwt-signing.key").write_text(
        "test-only", encoding="utf-8"
    )
    return data_root


def test_service_environment_matches_verified_live_layout(tmp_path: Path, monkeypatch) -> None:
    root = _repository(tmp_path)
    data_root = _program_data(tmp_path)
    for key in (
        "CLEANROOM_PORT",
        "CLEANROOM_DB_PATH",
        "CLEANROOM_DATA_ROOT",
        "CLEANROOM_TOOLS_DIR",
        "CLEANROOM_USER_CONFIG_ROOT",
        "CLEANROOM_JWT_SECRET_FILE",
        "CLEANROOM_CREDENTIAL_STORE_FILE",
        "CLEANROOM_CREDENTIAL_DPAPI_SCOPE",
        "CLEANROOM_OPEN_PANEL",
        "CLEANROOM_SKIP_STARTUP_AI",
        "CLEANROOM_SKIP_ICECAST_METADATA",
        "RADIOTEDU_PROCESS_ISOLATED_WORKERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    values = configure_environment(root)
    assert values["CLEANROOM_PORT"] == "18110"
    assert Path(values["CLEANROOM_DB_PATH"]) == data_root / "cleanroom.db"
    assert Path(values["CLEANROOM_DATA_ROOT"]) == data_root
    assert values["CLEANROOM_TOOLS_DIR"].endswith("backend-test\\tools")
    assert values["CLEANROOM_CREDENTIAL_DPAPI_SCOPE"] == "machine"
    assert values["RADIOTEDU_PROCESS_ISOLATED_WORKERS"] == "1"


def test_service_environment_fails_closed_without_database(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path)
    data_root = _program_data(tmp_path)
    (data_root / "cleanroom.db").unlink()
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    with pytest.raises(RuntimeError, match="migrated database"):
        configure_environment(root)
