from pathlib import Path

from tools.import_rtmd_service_configs import (
    _apply_overrides,
    install,
    parse_raw_files,
)


def test_parse_raw_files_keeps_exact_blocks():
    payload = (
        "RAW FILE: C:\\one\\.env\nA=1\nB=2\n"
        "RAW FILE: C:\\two\\.env\nC=3\n"
    )
    parsed = parse_raw_files(payload)
    assert parsed[r"c:\one\.env"] == "A=1\nB=2\n"
    assert parsed[r"c:\two\.env"] == "C=3\n"


def test_parse_raw_files_removes_archive_frame_delimiters():
    payload = (
        "RAW FILE: C:\\one\\.env\n"
        "================================================================================\n"
        "A=1\n"
        "\n"
        "================================================================================\n"
    )
    parsed = parse_raw_files(payload)
    assert parsed[r"c:\one\.env"] == "A=1\n"


def test_apply_overrides_replaces_and_appends_without_touching_other_values():
    content = "SECRET=keep-me\nMUSIC_ROOT=old\n"
    updated = _apply_overrides(
        content,
        {"MUSIC_ROOT": r"E:\Radio", "NEW_SAFE_VALUE": "false"},
    )
    assert "SECRET=keep-me" in updated
    assert r"MUSIC_ROOT=E:\Radio" in updated
    assert "NEW_SAFE_VALUE=false" in updated


def test_install_writes_only_selected_configs_without_echoing_values(tmp_path):
    rt_md = tmp_path / "rt.md"
    service_root = tmp_path / "services"
    rt_md.write_text(
        "RAW FILE: C:\\Users\\tedu\\Desktop\\juke-local\\media-agent\\.env\n"
        + "\n".join(f"JUKE_{index}=secret-{index}" for index in range(6))
        + "\nRAW FILE: C:\\Users\\tedu\\Desktop\\voting\\rtjukebox\\tools\\local-voting-agent\\.env\n"
        + "\n".join(f"VOTE_{index}=secret-{index}" for index in range(7))
        + "\n",
        encoding="utf-8",
    )

    report = install(rt_md, service_root)

    assert report["ok"] is True
    assert [row["variable_count"] for row in report["installed"]] == [6, 7]
    assert (
        service_root / "radiotedu-jukebox" / "media-agent" / ".env"
    ).read_text(encoding="utf-8").startswith("JUKE_0=secret-0")
    assert (
        service_root
        / "radiotedu-voting"
        / "tools"
        / "local-voting-agent"
        / ".env"
    ).read_text(encoding="utf-8").startswith("VOTE_0=secret-0")
