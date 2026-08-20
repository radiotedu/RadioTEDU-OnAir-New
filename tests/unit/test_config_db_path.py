import importlib
from pathlib import Path
import sys

import app.config as config


def test_get_db_path_default_is_not_cwd_dependent(monkeypatch, tmp_path):
    monkeypatch.delenv("CLEANROOM_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    importlib.reload(config)

    expected = (Path(config.__file__).resolve().parents[1] / "data" / "cleanroom.db").resolve()
    assert config.get_db_path() == expected


def test_get_db_path_uses_programdata_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delenv("CLEANROOM_DB_PATH", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "programdata"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    importlib.reload(config)

    expected = (
        Path(str(tmp_path / "programdata"))
        / "RadioTEDU"
        / "OnAir"
        / "cleanroom.db"
    ).resolve()
    assert config.get_db_path() == expected


def test_get_db_path_does_not_mutate_or_adopt_legacy_install_data(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CLEANROOM_DB_PATH", raising=False)
    local_app_data = tmp_path / "localappdata"
    legacy_db = local_app_data / "RadioTEDU Broadcast Room" / "data" / "cleanroom.db"
    legacy_db.parent.mkdir(parents=True)
    legacy_db.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "programdata"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    importlib.reload(config)

    assert config.get_db_path() == (
        tmp_path / "programdata" / "RadioTEDU" / "OnAir" / "cleanroom.db"
    ).resolve()
    assert legacy_db.read_text(encoding="utf-8") == "legacy"
