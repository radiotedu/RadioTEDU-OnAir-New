import os
from pathlib import Path

from scripts import onair_platform


def test_explicit_test_root_overrides_inherited_production_paths(tmp_path, monkeypatch):
    keys = ('CLEANROOM_DATA_ROOT', 'CLEANROOM_DB_PATH', 'CLEANROOM_USER_CONFIG_ROOT',
            'CLEANROOM_CREDENTIAL_STORE_FILE', 'CLEANROOM_CREDENTIAL_KEY_FILE', 'CLEANROOM_JWT_SECRET_FILE')
    for key in keys:
        monkeypatch.setenv(key, str(tmp_path / 'production' / key))
    isolated = (tmp_path / 'isolated').resolve()
    assert onair_platform.configure_environment(isolated) == isolated
    for key in keys:
        assert Path(os.environ[key]).is_relative_to(isolated)
    assert not isolated.exists()  # Configuration alone does not start or migrate anything.


def test_service_paths_are_preserved_without_an_explicit_new_root(tmp_path, monkeypatch):
    production = str(tmp_path / 'operator.db')
    monkeypatch.setenv('CLEANROOM_DB_PATH', production)
    onair_platform.configure_environment()
    assert os.environ['CLEANROOM_DB_PATH'] == production
