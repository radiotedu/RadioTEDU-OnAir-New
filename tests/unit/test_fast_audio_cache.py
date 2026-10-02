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


def test_consumed_non_system_drive_cache_entry_is_reused_without_copy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    cache = tmp_path / "cache"
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(cache))
    monkeypatch.setattr(ffmpeg_pipeline, "request_fast_audio_cache_prune", lambda: None)
    monkeypatch.setattr(
        ffmpeg_pipeline.os.path,
        "splitdrive",
        lambda path: ("H:", str(path)),
    )

    copied = []
    original_copy = ffmpeg_pipeline.shutil.copy2

    def record_copy(src, dst):
        copied.append(src)
        return original_copy(src, dst)

    monkeypatch.setattr(ffmpeg_pipeline.shutil, "copy2", record_copy)
    cached_uri = ffmpeg_pipeline._resolve_fast_cached_uri(str(source))
    assert Path(cached_uri).is_file()
    assert ffmpeg_pipeline.release_fast_cached_uri(
        str(source), delay_seconds=0
    ) is True

    assert ffmpeg_pipeline._resolve_fast_cached_uri(str(source)) == cached_uri
    assert Path(cached_uri).read_bytes() == b"audio"
    assert copied == [str(source)]


def test_queue_polling_reuses_warm_cache_without_threads_or_metadata_churn(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    cache = tmp_path / "cache"
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(cache))
    monkeypatch.setattr(ffmpeg_pipeline.os.path, "splitdrive", lambda p: ("H:", str(p)))
    monkeypatch.setattr(ffmpeg_pipeline, "request_fast_audio_cache_prune", lambda: None)
    clock = [10.0]
    monkeypatch.setattr(ffmpeg_pipeline.time, "monotonic", lambda: clock[0])
    touches = []
    original_utime = ffmpeg_pipeline.os.utime

    def record_utime(p, *args, **kwargs):
        touches.append(p)
        return original_utime(p, *args, **kwargs)

    monkeypatch.setattr(ffmpeg_pipeline.os, "utime", record_utime)
    cached_uri = ffmpeg_pipeline._resolve_fast_cached_uri(str(source))
    # copy2 preserves timestamps, then the cache gets one forced LRU touch.
    initial_count = len(touches)

    def unexpected_thread(*args, **kwargs):
        raise AssertionError("Warm cache prefetch must not start a worker")

    monkeypatch.setattr(ffmpeg_pipeline.threading, "Thread", unexpected_thread)
    for _ in range(100):
        ffmpeg_pipeline.prefetch_fast_cached_uri(str(source))
        assert ffmpeg_pipeline._resolve_fast_cached_uri(str(source)) == cached_uri
    assert len(touches) == initial_count
    clock[0] += 61.0
    ffmpeg_pipeline.prefetch_fast_cached_uri(str(source))
    assert len(touches) == initial_count + 1


def test_prune_requests_are_coalesced_after_finished_cleanup(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(ffmpeg_pipeline.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ffmpeg_pipeline, "_FAST_AUDIO_CACHE_LAST_PRUNE_STARTED", None)
    monkeypatch.setattr(ffmpeg_pipeline, "_FAST_AUDIO_CACHE_PRUNE_IN_FLIGHT", False)
    cleanups = []

    def prune(**kwargs):
        cleanups.append(clock[0])
        return {"ok": True, "after_bytes": 0, "target_bytes": 1, "deleted_files": 0}

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(ffmpeg_pipeline, "prune_fast_audio_cache", prune)
    monkeypatch.setattr(ffmpeg_pipeline.threading, "Thread", ImmediateThread)
    for _ in range(100):
        ffmpeg_pipeline.request_fast_audio_cache_prune()
    clock[0] = 59.0
    ffmpeg_pipeline.request_fast_audio_cache_prune()
    assert cleanups == [0.0]
    clock[0] = 60.0
    ffmpeg_pipeline.request_fast_audio_cache_prune()
    assert cleanups == [0.0, 60.0]


def test_cold_cache_does_not_block_decoder_start_with_whole_file_copy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ffmpeg_pipeline.os.path, "splitdrive", lambda p: ("H:", str(p)))
    prefetched = []
    monkeypatch.setattr(ffmpeg_pipeline, "prefetch_fast_cached_uri", prefetched.append)

    def unexpected_copy(*args, **kwargs):
        raise AssertionError("Decoder handoff must not synchronously copy media")

    monkeypatch.setattr(ffmpeg_pipeline.shutil, "copy2", unexpected_copy)
    args = ffmpeg_pipeline._build_input_args(str(source), realtime=True)
    assert args[-2:] == ["-i", str(source)]
    assert prefetched == [str(source)]


def test_recreated_cache_entry_gets_fresh_lru_timestamp(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    old_time = time.time() - 3600
    os.utime(source, (old_time, old_time))
    monkeypatch.setattr(ffmpeg_pipeline, "FAST_AUDIO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ffmpeg_pipeline.os.path, "splitdrive", lambda p: ("H:", str(p)))
    monkeypatch.setattr(ffmpeg_pipeline, "request_fast_audio_cache_prune", lambda: None)
    cached_uri = ffmpeg_pipeline._resolve_fast_cached_uri(str(source))
    Path(cached_uri).unlink()
    assert ffmpeg_pipeline._resolve_fast_cached_uri(str(source)) == cached_uri
    assert Path(cached_uri).stat().st_mtime > old_time + 3000
