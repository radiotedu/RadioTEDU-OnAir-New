import json
import os
from pathlib import Path

import pytest

import app.api.library_automation as library_automation
import app.services.unified_media_folder as unified_media_folder
from app.services.unified_media_folder import (
    LAYOUT_DIRECTORIES,
    UnifiedMediaFolderError,
    UnifiedMediaFolderService,
)


def _write_source_map(root: Path, payload: dict) -> None:
    path = root / "Manifests" / "source-map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_map() -> dict:
    return {
        "version": 1,
        "views": [
            {
                "view": "broadcast",
                "sources": [
                    {
                        "source": "Imports/Broadcast Source",
                        "destination": "primary",
                        "language": "Turkish",
                        "ignored_token": "never-persist-this",
                    }
                ],
            },
            {
                "view": "juke_non_turkish",
                "sources": [
                    {
                        "source": "Imports/Juke Source",
                        "language": "English",
                    }
                ],
            },
        ],
    }


def test_fixed_layout_and_hardlink_views_publish_without_media_copies(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    assert all((root / relative).is_dir() for relative in LAYOUT_DIRECTORIES)

    broadcast_source = root / "Imports" / "Broadcast Source"
    juke_source = root / "Imports" / "Juke Source"
    broadcast_source.mkdir()
    juke_source.mkdir()
    original = broadcast_source / "station.mp3"
    original.write_bytes(b"radio-bytes")
    (juke_source / "international.mp3").write_bytes(b"juke-bytes")
    _write_source_map(root, _valid_map())

    result = service.refresh()

    broadcast_view = root / "Broadcast" / "primary" / "station.mp3"
    juke_view = root / "Juke" / "Non-Turkish" / "international.mp3"
    assert result["views"]["broadcast"] == 1
    assert result["views"]["juke_non_turkish"] == 1
    assert broadcast_view.read_bytes() == b"radio-bytes"
    assert original.stat().st_ino == broadcast_view.stat().st_ino
    assert juke_view.stat().st_ino == (juke_source / "international.mp3").stat().st_ino

    manifest = json.loads((root / "Manifests" / "unified-media-manifest.json").read_text(encoding="utf-8"))
    assert manifest["hardlink_only"] is True
    assert {entry["language"] for entry in manifest["entries"]} == {"Turkish", "English"}
    assert "never-persist-this" not in json.dumps(manifest)
    assert "ignored_token" not in json.dumps(manifest)
    status = service.status()
    assert status["last_refresh_at"]
    assert status["last_error"] == ""


def test_refresh_preserves_operator_dropins_and_keeps_a_recoverable_backup(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    old_view_file = root / "Broadcast" / "old.mp3"
    old_view_file.write_bytes(b"old")
    source = root / "Imports" / "Broadcast Source"
    source.mkdir()
    (source / "new.mp3").write_bytes(b"new")
    _write_source_map(
        root,
        {
            "version": 1,
            "views": [
                {
                    "view": "broadcast",
                    "sources": [
                        {"source": "Imports/Broadcast Source", "language": "Turkish"}
                    ],
                }
            ],
        },
    )

    service.refresh()

    assert old_view_file.exists()
    assert (root / "Broadcast" / "new.mp3").read_bytes() == b"new"
    manifest = json.loads((root / "Manifests" / "unified-media-manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["broadcast"] == {"generated": 1, "operator": 1}
    backups = list((root / "Backups").glob("unified-media-*/broadcast/old.mp3"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old"


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {
                "version": 1,
                "views": [
                    {
                        "view": "broadcast",
                        "sources": [{"source": "../outside", "language": "Turkish"}],
                    }
                ],
            },
            "source_must_be_relative_and_contained",
        ),
        (
            {
                "version": 1,
                "views": [
                    {
                        "view": "broadcast",
                        "sources": [{"source": "Imports/Source"}],
                    }
                ],
            },
            "source_map_source_and_language_required",
        ),
    ],
)
def test_source_map_rejects_outside_roots_and_never_guesses_language(
    tmp_path, payload, expected
):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    (root / "Imports" / "Source").mkdir()
    _write_source_map(root, payload)

    with pytest.raises(UnifiedMediaFolderError, match=expected):
        service.refresh()


def test_source_map_rejects_duplicate_view_destination_before_publish(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    for name in ("A", "B"):
        folder = root / "Imports" / name
        folder.mkdir()
        (folder / "same.mp3").write_bytes(name.encode())
    _write_source_map(
        root,
        {
            "version": 1,
            "views": [
                {
                    "view": "broadcast",
                    "sources": [
                        {"source": "Imports/A", "language": "Turkish"},
                        {"source": "Imports/B", "language": "Turkish"},
                    ],
                }
            ],
        },
    )

    with pytest.raises(UnifiedMediaFolderError, match="source_map_target_collision"):
        service.refresh()
    assert not list((root / "Broadcast").rglob("*.mp3"))


def test_failed_refresh_records_a_safe_operator_error(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    _write_source_map(
        root,
        {
            "version": 1,
            "views": [
                {
                    "view": "broadcast",
                    "sources": [{"source": "../private", "language": "Turkish"}],
                }
            ],
        },
    )

    with pytest.raises(UnifiedMediaFolderError, match="source_must_be_relative_and_contained"):
        service.refresh()

    status = service.status()
    assert status["last_refresh_at"]
    assert status["last_error"] == "source_must_be_relative_and_contained"
    assert "private" not in json.dumps(status)


def test_publish_failure_restores_prior_views(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    staging = root / "Manifests" / ".unified-media" / "staging" / "rollback-test"
    rollback = root / "Backups" / "rollback-test"
    for view, old, new in (("Broadcast", b"old-b", b"new-b"), ("Voting", b"old-v", b"new-v")):
        (root / view / "track.mp3").write_bytes(old)
        (staging / {"Broadcast": "broadcast", "Voting": "voting"}[view]).mkdir(parents=True, exist_ok=True)
        (staging / {"Broadcast": "broadcast", "Voting": "voting"}[view] / "track.mp3").write_bytes(new)
    rollback.mkdir(parents=True)
    original_replace = unified_media_folder.os.replace
    calls = {"count": 0}

    def fail_during_second_view(source, destination):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError("simulated publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(unified_media_folder.os, "replace", fail_during_second_view)
    with pytest.raises(UnifiedMediaFolderError, match="atomic_publish_failed"):
        service._publish_views("rollback-test", staging, rollback, {"broadcast": [], "voting": []})
    assert (root / "Broadcast" / "track.mp3").read_bytes() == b"old-b"
    assert (root / "Voting" / "track.mp3").read_bytes() == b"old-v"


def test_long_source_and_staged_destination_paths_publish_as_hardlinks(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    source = root / "Imports" / "Long Source"
    nested = source.joinpath(*(["segment-" + "x" * 36] * 8))
    service._mkdir(nested, parents=True, exist_ok=False)
    original = nested / "track.mp3"
    with open(service._io_path(original), "wb") as handle:
        handle.write(b"long-path-track")
    assert len(str(original)) > 260
    _write_source_map(
        root,
        {
            "version": 1,
            "views": [
                {
                    "view": "broadcast",
                    "sources": [{"source": "Imports/Long Source", "language": "Turkish"}],
                }
            ],
        },
    )

    result = service.refresh()

    linked = root / "Broadcast" / original.relative_to(source)
    assert result["views"]["broadcast"] == 1
    with open(service._io_path(linked), "rb") as handle:
        assert handle.read() == b"long-path-track"
    assert os.stat(service._io_path(linked)).st_ino == os.stat(service._io_path(original)).st_ino
    assert len(str(linked)) > 260


def test_windows_extended_path_helper_and_staging_cleanup_are_bounded(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    assert UnifiedMediaFolderService._windows_io_path(
        "C:\\RadioTEDU Media\\" + "x" * 280, windows=True
    ).startswith("\\\\?\\C:\\")
    assert UnifiedMediaFolderService._windows_io_path(
        "\\\\server\\radio\\" + "x" * 280, windows=True
    ).startswith("\\\\?\\UNC\\server\\")

    staging = root / "Manifests" / ".unified-media" / "staging" / "run-1"
    staging.mkdir()
    (staging / "partial.mp3").write_bytes(b"partial")
    outside = root / "Backups" / "must-remain"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    service._cleanup_staging(outside)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    service._cleanup_staging(staging)
    assert not staging.exists()


def test_authenticated_api_handlers_publish_and_request_existing_rescan(
    tmp_path, monkeypatch
):
    root = tmp_path / "RadioTEDU Media"
    service = UnifiedMediaFolderService(root)
    service.ensure_layout()
    source = root / "Imports" / "Broadcast Source"
    source.mkdir()
    (source / "song.mp3").write_bytes(b"song")
    _write_source_map(
        root,
        {
            "version": 1,
            "views": [
                {
                    "view": "broadcast",
                    "sources": [
                        {"source": "Imports/Broadcast Source", "language": "Turkish"}
                    ],
                }
            ],
        },
    )

    class Watcher:
        def request_rescan(self):
            return 3

        @staticmethod
        def snapshot():
            return {"running": False, "profiles": []}

    monkeypatch.setattr(library_automation, "get_unified_media_folder_service", lambda: service)
    monkeypatch.setattr(library_automation, "get_managed_library_watcher", lambda: Watcher())

    status = library_automation.unified_media_status(_user={})
    refreshed = library_automation.refresh_unified_media(
        library_automation.UnifiedMediaRefreshPayload(), _user={}
    )

    assert status["source_map_configured"] is True
    assert refreshed["ok"] is True
    assert refreshed["library_rescan_queued_profiles"] == 3
    assert (root / "Broadcast" / "song.mp3").is_file()
