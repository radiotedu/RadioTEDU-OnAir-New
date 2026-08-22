import os
import time
from pathlib import Path

from app.audio import ffmpeg_pipeline


def test_prune_fast_audio_cache_reaches_limit_and_protects_recent_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(tmp_path))
    old_files = []
    for index in range(4):
        path = tmp_path / f"old-{index}.mp3"
        path.write_bytes(b"x" * 1024)
        old_files.append(path)
    fresh = tmp_path / "fresh.mp3"
    fresh.write_bytes(b"y" * 1024)
    old_timestamp = time.time() - 3600
    for path in old_files:
        os.utime(path, (old_timestamp, old_timestamp))
    os.chmod(old_files[0], 0o444)

    result = ffmpeg_pipeline.prune_fast_audio_cache(
        max_bytes=1024,
        min_age_seconds=60,
        max_deletions=100,
    )

    assert result["ok"] is True
    assert result["deleted_files"] == 4
    assert result["after_bytes"] == 1024
    assert fresh.is_file()


def test_consumed_non_system_drive_cache_entry_is_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    cache = tmp_path / "cache"
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(cache))
    monkeypatch.setattr(
        ffmpeg_pipeline.os.path,
        "splitdrive",
        lambda path: ("H:", str(path)),
    )

    cached_uri = ffmpeg_pipeline._resolve_fast_cached_uri(str(source))
    assert Path(cached_uri).is_file()
    assert ffmpeg_pipeline.release_fast_cached_uri(
        str(source), delay_seconds=0
    ) is True

    deadline = time.time() + 2
    while Path(cached_uri).exists() and time.time() < deadline:
        time.sleep(0.01)
    assert not Path(cached_uri).exists()
