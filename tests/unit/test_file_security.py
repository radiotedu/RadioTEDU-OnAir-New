import asyncio
import io
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.api.legacy import _resolve_media_path
from app.file_security import resolve_under_root, write_upload_to_path
from app.media_paths import resolve_runtime_media_path


def test_resolve_under_root_rejects_parent_escape(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir(parents=True, exist_ok=True)

    assert resolve_under_root(root, "../secret.txt") is None


def test_resolve_runtime_media_path_does_not_escape_uploads_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    resolved = resolve_runtime_media_path("../secret.txt")

    assert resolved == "../secret.txt"


def test_legacy_media_path_does_not_serve_db_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    db_file = tmp_path / "cleanroom.db"
    db_file.write_text("sqlite", encoding="utf-8")

    assert _resolve_media_path("cleanroom.db") is None


def test_write_upload_to_path_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4")
    upload = UploadFile(filename="clip.mp3", file=io.BytesIO(b"123456"))
    destination = tmp_path / "clip.mp3"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(write_upload_to_path(upload, destination, max_bytes=4))

    assert exc_info.value.status_code == 413
    assert not destination.exists()
