from __future__ import annotations

import json
import time
from pathlib import Path

from app.services.ai_cache_index import AnnouncementCacheIndex


def _write_announcement(cache_dir: Path, number: int, dedupe_key: str) -> None:
    audio = cache_dir / f"announcement_{number}.wav"
    audio.write_bytes(b"RIFF-test")
    (cache_dir / f"announcement_{number}.json").write_text(
        json.dumps(
            {
                "cache_key": str(number),
                "dedupe_key": dedupe_key,
                "audio_path": str(audio),
                "tts_provider": "edge-tts",
            }
        ),
        encoding="utf-8",
    )


def test_large_initial_cache_lookup_never_blocks_scheduler_on_full_scan(
    tmp_path, monkeypatch
):
    for number in range(300):
        _write_announcement(tmp_path, number, f"intro:{number}")
    index = AnnouncementCacheIndex(tmp_path)
    original_scan = index._scan

    def slow_scan():
        time.sleep(0.3)
        return original_scan()

    monkeypatch.setattr(index, "_scan", slow_scan)
    started = time.monotonic()
    assert index.lookup("intro:299", expected_tts_provider="edge-tts") is None
    assert time.monotonic() - started < 0.2

    # Windows antivirus/indexing can make hundreds of metadata reads slow; the
    # acceptance property is that this work stays off the scheduler thread.
    deadline = time.monotonic() + 30.0
    payload = None
    while time.monotonic() < deadline:
        payload = index.lookup("intro:299", expected_tts_provider="edge-tts")
        if payload is not None:
            break
        time.sleep(0.02)
    assert payload is not None


def test_register_is_immediately_visible_during_background_build(tmp_path):
    index = AnnouncementCacheIndex(tmp_path)
    audio = tmp_path / "new.wav"
    audio.write_bytes(b"RIFF-test")
    payload = {
        "cache_key": "new",
        "dedupe_key": "intro:new",
        "audio_path": str(audio),
        "tts_provider": "local-qwen-tts",
    }
    index.register(payload)
    assert index.lookup(
        "intro:new", expected_tts_provider="local-qwen-tts"
    )["cache_key"] == "new"
