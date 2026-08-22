"""Bound RadioTEDU's disposable read-ahead cache beside live playout."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024 * 1024)
    parser.add_argument("--min-age-seconds", type=float, default=900.0)
    parser.add_argument("--max-deletions", type=int, default=2000)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    from app.audio.ffmpeg_pipeline import FAST_AUDIO_CACHE_DIR, prune_fast_audio_cache

    cache_root = Path(FAST_AUDIO_CACHE_DIR).resolve()
    expected_root = Path(r"C:\ProgramData\RadioTEDU\OnAir\FastAudioCache").resolve()
    if cache_root != expected_root:
        raise RuntimeError(f"Refusing unexpected cache root: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / ".prune.lock"
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() - lock_path.stat().st_mtime <= 1800:
                print(json.dumps({"ok": True, "status": "already_running"}))
                return 0
            lock_path.unlink(missing_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        result = prune_fast_audio_cache(
            max_bytes=args.max_bytes,
            min_age_seconds=args.min_age_seconds,
            max_deletions=args.max_deletions,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if bool(result.get("ok")) else 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
