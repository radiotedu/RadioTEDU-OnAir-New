from __future__ import annotations

import array
import math
import subprocess
import sys
from pathlib import Path

from app.runtime_paths import resolve_binary


SAMPLE_RATE = 11025
HOP_SIZE = 512
MIN_BPM = 45.0
MAX_BPM = 210.0


def _analysis_creation_flags() -> int:
    # Decoder work is maintenance. Live playout and encoder FFmpeg processes
    # run Above Normal and must always win CPU scheduling on Windows.
    return 0x00004000 if sys.platform == "win32" else 0


def _decode_mono_pcm(file_path: str, max_seconds: int = 90) -> array.array:
    ffmpeg = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable; BPM analysis cannot decode audio")
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-t",
            str(max(15, min(180, int(max_seconds)))),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ],
        capture_output=True,
        timeout=max(45, int(max_seconds) + 30),
        check=False,
        creationflags=_analysis_creation_flags(),
    )
    if result.returncode != 0 or len(result.stdout) < SAMPLE_RATE * 5:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail[:300] or "audio decode produced too little data")
    samples = array.array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def estimate_bpm_from_samples(samples: array.array) -> tuple[float, float]:
    """Estimate tempo from a mono PCM onset envelope.

    This intentionally uses a small, deterministic autocorrelation estimator so
    the radio can analyze files without a heavyweight scientific runtime.
    Confidence below 0.08 is treated as unknown by the caller.
    """
    if len(samples) < SAMPLE_RATE * 5:
        return 0.0, 0.0
    frame_size = HOP_SIZE * 2
    energies: list[float] = []
    for start in range(0, len(samples) - frame_size, HOP_SIZE):
        frame = samples[start : start + frame_size]
        energies.append(sum(abs(value) for value in frame) / frame_size)
    if len(energies) < 64:
        return 0.0, 0.0
    onsets = [0.0]
    previous = energies[0]
    for energy in energies[1:]:
        delta = max(0.0, energy - previous)
        onsets.append(delta)
        previous = energy
    mean = sum(onsets) / len(onsets)
    variance = sum((value - mean) ** 2 for value in onsets) / len(onsets)
    if variance <= 1e-9:
        return 0.0, 0.0
    deviation = math.sqrt(variance)
    normalized = [(value - mean) / deviation for value in onsets]
    min_lag = max(2, int(60.0 * SAMPLE_RATE / (HOP_SIZE * MAX_BPM)))
    max_lag = min(len(normalized) // 3, int(60.0 * SAMPLE_RATE / (HOP_SIZE * MIN_BPM)) + 1)
    scored: list[tuple[float, int]] = []
    for lag in range(min_lag, max_lag + 1):
        correlation = sum(
            normalized[index] * normalized[index - lag]
            for index in range(lag, len(normalized))
        ) / max(1, len(normalized) - lag)
        # Repeating beats at two periods strengthen the fundamental and reduce
        # the common half/double-tempo error.
        if lag * 2 <= max_lag:
            harmonic = sum(
                normalized[index] * normalized[index - lag * 2]
                for index in range(lag * 2, len(normalized))
            ) / max(1, len(normalized) - lag * 2)
            correlation += max(0.0, harmonic) * 0.35
        scored.append((correlation, lag))
    if not scored:
        return 0.0, 0.0
    scored.sort(reverse=True)
    best_score, best_lag = scored[0]
    second_score = next((score for score, lag in scored[1:] if abs(lag - best_lag) > 1), 0.0)
    confidence = max(0.0, min(1.0, (best_score - max(0.0, second_score)) / max(0.25, abs(best_score))))
    bpm = 60.0 * SAMPLE_RATE / (HOP_SIZE * best_lag)
    while bpm < 70.0 and bpm * 2.0 <= MAX_BPM:
        bpm *= 2.0
    while bpm > 180.0 and bpm / 2.0 >= MIN_BPM:
        bpm /= 2.0
    return round(bpm, 1), round(confidence, 3)


def analyze_bpm(file_path: str, max_seconds: int = 90) -> tuple[float, float]:
    return estimate_bpm_from_samples(_decode_mono_pcm(file_path, max_seconds=max_seconds))
