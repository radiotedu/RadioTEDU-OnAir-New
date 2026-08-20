from pathlib import Path

import pytest

from app.main import app
from app.version import PRODUCT_VERSION
from scripts.release_bundle import build_release_manifest


def test_repository_version_drives_api_and_release_manifest():
    root = Path(__file__).resolve().parents[2]
    source_version = (root / "VERSION").read_text(encoding="utf-8").strip()

    assert source_version == "1.0.2"
    assert PRODUCT_VERSION == source_version
    assert app.version == source_version
    assert build_release_manifest()["version"] == source_version


def test_release_manifest_rejects_version_drift():
    with pytest.raises(ValueError, match="does not match VERSION"):
        build_release_manifest("9.9.9")


def test_build_and_installer_contracts_consume_version_source():
    root = Path(__file__).resolve().parents[2]
    backend_build = (root / "build_backend_onefile.ps1").read_text(encoding="utf-8")
    setup_build = (root / "installer" / "build_setup.ps1").read_text(
        encoding="utf-8"
    )
    setup = (root / "installer" / "RadioTEDUBroadcastRoomSetup.iss").read_text(
        encoding="utf-8"
    )
    desktop_props = (root / "desktop" / "Directory.Build.props").read_text(
        encoding="utf-8"
    )

    assert 'Join-Path $root "VERSION"' in backend_build
    assert '--add-data ".\\VERSION;."' in backend_build
    assert "product_version" in backend_build
    assert "git_commit" in backend_build
    assert "git_tracked_tree_dirty" in backend_build
    assert 'Join-Path $root "..\\VERSION"' in setup_build
    assert "does not match product VERSION" in setup_build
    assert "#error AppVersion must be supplied" in setup
    assert "ReadAllText('$(MSBuildThisFileDirectory)..\\VERSION')" in desktop_props
