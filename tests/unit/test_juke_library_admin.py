import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.services.juke_library_admin import (
    configured_roots,
    list_library,
    restore_item,
    retire_item,
    store_upload,
)


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    primary = tmp_path / "primary"
    overflow = tmp_path / "overflow"
    primary.mkdir()
    overflow.mkdir()
    config = tmp_path / "juke.env"
    config.write_text(
        "\n".join(
            [
                f"LOCAL_MUSIC_ROOT={primary}",
                f"LOCAL_MUSIC_OVERFLOW_ROOT={overflow}",
                "MEDIA_AGENT_REQUEST_SECRET=not-returned-by-library-admin",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config, primary, overflow


def test_library_listing_is_stable_and_never_returns_secrets(tmp_path: Path):
    config, primary, overflow = _config(tmp_path)
    (primary / "Zulu.mp3").write_bytes(b"ID3-z")
    (primary / "alpha.mp3").write_bytes(b"ID3-a")
    (overflow / "middle.flac").write_bytes(b"fLaC")

    first = list_library(config, limit=50)
    second = list_library(config, limit=50)

    assert first == second
    assert [item["relative_path"] for item in first["items"]] == [
        "alpha.mp3",
        "Zulu.mp3",
        "middle.flac",
    ]
    assert [item["id"] for item in configured_roots(config)] == [
        "primary",
        "overflow",
    ]
    assert "not-returned-by-library-admin" not in repr(first)


def test_retire_and_restore_are_recoverable_and_root_bounded(tmp_path: Path):
    config, primary, _overflow = _config(tmp_path)
    song = primary / "Genre" / "song.mp3"
    song.parent.mkdir()
    song.write_bytes(b"ID3")

    retired = retire_item(
        config,
        root_id="primary",
        relative_path="Genre/song.mp3",
    )
    assert retired["recoverable"] is True
    assert not song.exists()
    trash = list_library(config, include_trash=True, limit=50)
    assert trash["items"][0]["original_relative_path"] == "Genre/song.mp3"

    restored = restore_item(
        config,
        root_id="primary",
        trash_path=retired["trash_path"],
    )
    assert restored["relative_path"] == "Genre/song.mp3"
    assert song.read_bytes() == b"ID3"

    with pytest.raises(HTTPException) as exc:
        retire_item(config, root_id="primary", relative_path="../outside.mp3")
    assert exc.value.status_code == 400


def test_upload_is_atomic_and_duplicate_names_are_rejected(tmp_path: Path, monkeypatch):
    config, primary, _overflow = _config(tmp_path)
    from app.services import juke_library_admin as library

    monkeypatch.setattr(
        library,
        "_validate_audio",
        lambda _path: {"codec": "mp3", "sample_rate": 44100, "channels": 2, "duration_seconds": 1.0},
    )
    upload = UploadFile(filename="New Song.mp3", file=BytesIO(b"ID3-new"))
    stored = asyncio.run(
        store_upload(
            config,
            root_id="primary",
            relative_folder="Fresh",
            upload=upload,
        )
    )
    assert stored["relative_path"] == "Fresh/New_Song.mp3"
    assert (primary / "Fresh" / "New_Song.mp3").read_bytes() == b"ID3-new"
    assert not list((primary / "Fresh").glob("*.tmp"))
    assert not list((primary / "Fresh").glob("*.radiotedu-upload.lock"))

    duplicate = UploadFile(filename="New Song.mp3", file=BytesIO(b"ID3-other"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            store_upload(
                config,
                root_id="primary",
                relative_folder="Fresh",
                upload=duplicate,
            )
        )
    assert exc.value.status_code == 409


def test_invalid_audio_is_rejected_without_leaving_a_file(tmp_path: Path, monkeypatch):
    config, primary, _overflow = _config(tmp_path)
    from app.services import juke_library_admin as library

    def reject(_path):
        raise HTTPException(status_code=400, detail="invalid_juke_audio_file")

    monkeypatch.setattr(library, "_validate_audio", reject)
    invalid = UploadFile(filename="not-a-song.mp3", file=BytesIO(b"not audio"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            store_upload(
                config,
                root_id="primary",
                relative_folder="Fresh",
                upload=invalid,
            )
        )
    assert exc.value.status_code == 400
    assert not (primary / "Fresh" / "not-a-song.mp3").exists()
    assert not list((primary / "Fresh").glob("*.tmp"))
    assert not list((primary / "Fresh").glob("*.radiotedu-upload.lock"))


def test_listing_skips_a_file_that_disappears_during_live_scan(tmp_path: Path, monkeypatch):
    config, primary, _overflow = _config(tmp_path)
    stable = primary / "stable.mp3"
    vanished = primary / "vanished.mp3"
    stable.write_bytes(b"ID3-stable")
    vanished.write_bytes(b"ID3-vanished")

    from app.services import juke_library_admin as library

    original = library._media_item

    def race(root_id, root, path):
        if path.name == "vanished.mp3":
            raise FileNotFoundError(path)
        return original(root_id, root, path)

    monkeypatch.setattr(library, "_media_item", race)
    result = list_library(config, limit=50)

    assert [item["relative_path"] for item in result["items"]] == ["stable.mp3"]
    assert result["skipped_unreadable"] == 1
