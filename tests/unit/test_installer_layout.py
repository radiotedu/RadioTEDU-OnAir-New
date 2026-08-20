from pathlib import Path


INSTALLER = (
    Path(__file__).resolve().parents[2]
    / "installer"
    / "RadioTEDUBroadcastRoomSetup.iss"
)


def test_installer_requires_program_files_and_preserves_programdata():
    source = INSTALLER.read_text(encoding="utf-8")

    assert r"DefaultDirName={commonpf}\RadioTEDU\OnAir" in source
    assert "PrivilegesRequired=admin" in source
    assert "PrivilegesRequiredOverridesAllowed" not in source
    assert r"{commonappdata}\RadioTEDU\OnAir" in source
    assert "uninsneveruninstall" in source


def test_installer_precreates_managed_media_categories():
    source = INSTALLER.read_text(encoding="utf-8")

    for category in (
        "Media\\Songs",
        "Media\\Jingles",
        "Media\\Station IDs",
        "Media\\Advertisements",
        "Media\\Recorded Shows",
    ):
        assert category in source
