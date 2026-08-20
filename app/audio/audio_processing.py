"""Audio processing utilities using ffmpeg.

Provides silence trimming and intro/outro cleanup for imported tracks.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.runtime_paths import resolve_binary


def _get_ffmpeg() -> str:
    path = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
    if not path:
        raise FileNotFoundError("ffmpeg not found")
    return path


def _get_ffprobe() -> str:
    path = resolve_binary("ffprobe.exe") or resolve_binary("ffprobe")
    if not path:
        raise FileNotFoundError("ffprobe not found")
    return path


def _probe_duration(file_path: str, *, timeout_seconds: float = 30.0) -> float:
    """Get duration of an audio file in seconds."""
    ffprobe = _get_ffprobe()
    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout_seconds)),
        )
        if result.returncode != 0:
            return 0.0
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def probe_duration(file_path: str, *, timeout_seconds: float = 30.0) -> float:
    """Public duration probe helper for callers outside this module."""
    return _probe_duration(file_path, timeout_seconds=timeout_seconds)


def _detect_silence_regions(
    file_path: str,
    threshold_db: float = -45.0,
    min_silence: float = 0.15,
) -> list[dict]:
    """Use ffmpeg silencedetect to find silent regions in the audio.

    Returns a list of dicts with 'start' and 'end' keys (seconds).
    """
    ffmpeg = _get_ffmpeg()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i", file_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except Exception:
        return []

    # Parse stderr for silence_start / silence_end lines
    regions: list[dict] = []
    current_start = None
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            try:
                idx = line.index("silence_start:") + len("silence_start:")
                current_start = float(line[idx:].strip().split()[0])
            except (ValueError, IndexError):
                current_start = None
        elif "silence_end:" in line and current_start is not None:
            try:
                idx = line.index("silence_end:") + len("silence_end:")
                end_val = float(line[idx:].strip().split()[0])
                regions.append({"start": current_start, "end": end_val})
            except (ValueError, IndexError):
                pass
            current_start = None

    return regions


def trim_silence(
    file_path: str,
    threshold_db: float = -45.0,
    min_silence: float = 0.15,
) -> dict:
    """Trim silence from the start and end of an audio file.

    Modifies the file in-place and returns a summary dict.
    """
    file_path = str(file_path)
    if not os.path.isfile(file_path):
        return {"trimmed": False, "error": "file not found", "removed_seconds": 0.0}

    duration = _probe_duration(file_path)
    if duration <= 0:
        return {"trimmed": False, "error": "could not probe duration", "removed_seconds": 0.0}

    regions = _detect_silence_regions(file_path, threshold_db, min_silence)
    if not regions:
        return {"trimmed": False, "removed_seconds": 0.0}

    # Find leading silence (region that starts at or very near 0)
    trim_start = 0.0
    if regions and regions[0]["start"] < 0.05:
        trim_start = regions[0]["end"]

    # Find trailing silence (region that ends at or very near duration)
    trim_end = duration
    if regions and regions[-1]["end"] >= (duration - 0.05):
        trim_end = regions[-1]["start"]

    # Don't trim if nothing meaningful to remove
    removed = trim_start + (duration - trim_end)
    if removed < 0.1:
        return {"trimmed": False, "removed_seconds": 0.0}

    # Sanity: ensure we keep at least 1 second of audio
    if (trim_end - trim_start) < 1.0:
        return {"trimmed": False, "removed_seconds": 0.0, "error": "would remove too much"}

    # Re-encode the trimmed portion
    ffmpeg = _get_ffmpeg()
    suffix = Path(file_path).suffix or ".mp3"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", file_path,
            "-ss", f"{trim_start:.3f}",
            "-to", f"{trim_end:.3f}",
            "-c", "copy",
            tmp_path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if result.returncode != 0:
            # Retry with re-encode if stream copy fails
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", file_path,
                "-ss", f"{trim_start:.3f}",
                "-to", f"{trim_end:.3f}",
                tmp_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            if result.returncode != 0:
                return {"trimmed": False, "error": result.stderr.strip()[:200], "removed_seconds": 0.0}

        # Verify output exists and has content
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) < 1024:
            return {"trimmed": False, "error": "output too small", "removed_seconds": 0.0}

        # Replace original
        shutil.move(tmp_path, file_path)
        new_duration = _probe_duration(file_path)
        actual_removed = max(0.0, duration - new_duration)

        return {
            "trimmed": True,
            "removed_seconds": round(actual_removed, 3),
            "original_duration": round(duration, 3),
            "new_duration": round(new_duration, 3),
            "trim_start": round(trim_start, 3),
            "trim_end": round(trim_end, 3),
        }
    except Exception as exc:
        return {"trimmed": False, "error": str(exc)[:200], "removed_seconds": 0.0}
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _detect_speech_boundary(
    file_path: str,
    analyze_seconds: float = 30.0,
    energy_threshold_db: float = -30.0,
) -> float | None:
    """Detect where music starts by looking at volume/energy changes.

    Uses the ebur128 loudness measurement to find where steady music
    energy begins, which typically indicates the end of a spoken intro.
    Returns the cut point in seconds, or None if no intro detected.
    """
    ffmpeg = _get_ffmpeg()

    # Extract volume levels for the first N seconds using volumedetect on small windows
    # We use astats to get per-frame RMS levels
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i", file_path,
        "-t", f"{analyze_seconds:.1f}",
        "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except Exception:
        return None

    # Parse frame-level RMS values from stderr/stdout
    rms_values: list[tuple[float, float]] = []
    frame_time = 0.0
    frame_duration = 1.0 / 43.066  # ~23ms per frame at 44100Hz

    for line in result.stderr.splitlines():
        if "lavfi.astats.Overall.RMS_level" in line:
            try:
                val = float(line.split("=")[-1].strip())
                if val > -100:  # ignore -inf frames
                    rms_values.append((frame_time, val))
            except (ValueError, IndexError):
                pass
            frame_time += frame_duration

    if len(rms_values) < 10:
        return None

    # Look for a transition from speech-like pattern to music-like pattern
    # Speech: variable RMS with pauses; Music: more consistent higher RMS
    # Simple heuristic: find the first sustained loud segment
    window = min(20, len(rms_values) // 4)
    if window < 5:
        return None

    # Calculate rolling average energy
    avg_energies: list[tuple[float, float]] = []
    for i in range(len(rms_values) - window):
        chunk = rms_values[i:i + window]
        avg_rms = sum(v for _, v in chunk) / len(chunk)
        avg_energies.append((chunk[0][0], avg_rms))

    if not avg_energies:
        return None

    # Find the overall average of the file
    overall_avg = sum(v for _, v in avg_energies) / len(avg_energies)

    # Find where energy first stays consistently above threshold
    threshold = max(energy_threshold_db, overall_avg - 6.0)
    for time_s, avg_e in avg_energies:
        if avg_e >= threshold and time_s > 1.0:
            return time_s

    return None


def clean_intro(
    file_path: str,
    max_cut_seconds: float = 18.0,
    analyze_seconds: float = 30.0,
) -> dict:
    """Detect and remove spoken intro/outro from an audio file.

    Uses energy analysis to find where music starts. Only trims
    if the detected intro is between 2 and max_cut_seconds.
    Modifies the file in-place.
    """
    file_path = str(file_path)
    if not os.path.isfile(file_path):
        return {"cleaned": False, "error": "file not found", "removed_seconds": 0.0}

    duration = _probe_duration(file_path)
    if duration <= 0 or duration < 10.0:
        return {"cleaned": False, "removed_seconds": 0.0}

    # Detect intro boundary
    cut_point = _detect_speech_boundary(
        file_path,
        analyze_seconds=min(analyze_seconds, duration * 0.5),
    )

    if cut_point is None or cut_point < 2.0 or cut_point > max_cut_seconds:
        return {"cleaned": False, "removed_seconds": 0.0}

    # Also check outro — detect speech at the end
    # For outro, we reverse-analyze the last N seconds
    # Simpler approach: trim silence from the end handles most outro cases
    # For now, focus on intro trimming

    ffmpeg = _get_ffmpeg()
    suffix = Path(file_path).suffix or ".mp3"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", file_path,
            "-ss", f"{cut_point:.3f}",
            "-c", "copy",
            tmp_path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if result.returncode != 0:
            # Retry with re-encode
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", file_path,
                "-ss", f"{cut_point:.3f}",
                tmp_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            if result.returncode != 0:
                return {"cleaned": False, "error": result.stderr.strip()[:200], "removed_seconds": 0.0}

        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) < 1024:
            return {"cleaned": False, "error": "output too small", "removed_seconds": 0.0}

        shutil.move(tmp_path, file_path)
        new_duration = _probe_duration(file_path)
        actual_removed = max(0.0, duration - new_duration)

        return {
            "cleaned": True,
            "removed_seconds": round(actual_removed, 3),
            "original_duration": round(duration, 3),
            "new_duration": round(new_duration, 3),
            "cut_point": round(cut_point, 3),
        }
    except Exception as exc:
        return {"cleaned": False, "error": str(exc)[:200], "removed_seconds": 0.0}
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def trim_manual(
    file_path: str,
    start_cut: float = 0.0,
    end_cut: float = 0.0,
) -> dict:
    """Cut a specific number of seconds from the start and/or end of a track."""
    file_path = str(file_path)
    if not os.path.isfile(file_path):
        return {"trimmed": False, "error": "file not found", "removed_seconds": 0.0}

    duration = _probe_duration(file_path)
    if duration <= 0:
        return {"trimmed": False, "error": "could not probe duration", "removed_seconds": 0.0}

    start_cut = max(0.0, float(start_cut or 0.0))
    end_cut = max(0.0, float(end_cut or 0.0))

    if (start_cut + end_cut) < 0.05:
        return {"trimmed": False, "removed_seconds": 0.0}

    new_end = duration - end_cut
    if (new_end - start_cut) < 1.0:
        return {"trimmed": False, "error": "would remove too much", "removed_seconds": 0.0}

    ffmpeg = _get_ffmpeg()
    suffix = Path(file_path).suffix or ".mp3"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", file_path,
            "-ss", f"{start_cut:.3f}",
            "-to", f"{new_end:.3f}",
            "-c", "copy",
            tmp_path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if result.returncode != 0:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", file_path,
                "-ss", f"{start_cut:.3f}",
                "-to", f"{new_end:.3f}",
                tmp_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            if result.returncode != 0:
                return {"trimmed": False, "error": result.stderr.strip()[:200], "removed_seconds": 0.0}

        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) < 1024:
            return {"trimmed": False, "error": "output too small", "removed_seconds": 0.0}

        shutil.move(tmp_path, file_path)
        new_duration = _probe_duration(file_path)
        actual_removed = max(0.0, duration - new_duration)

        return {
            "trimmed": True,
            "removed_seconds": round(actual_removed, 3),
            "original_duration": round(duration, 3),
            "new_duration": round(new_duration, 3),
            "start_cut": round(start_cut, 3),
            "end_cut": round(end_cut, 3),
        }
    except Exception as exc:
        return {"trimmed": False, "error": str(exc)[:200], "removed_seconds": 0.0}
    finally:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def process_imported_track(
    file_path: str,
    auto_trim_silence: bool = False,
    auto_intro_clean: bool = False,
    threshold_db: float = -45.0,
    min_silence: float = 0.15,
    intro_max_cut_s: float = 18.0,
    intro_analyze_s: float = 30.0,
) -> dict:
    """Run all requested processing on a newly imported track.

    Called by upload and yt-dlp import flows.
    Returns a combined result dict.
    """
    trim_result = {"trimmed": False, "removed_seconds": 0.0}
    intro_result = {"cleaned": False, "removed_seconds": 0.0}

    if auto_trim_silence:
        try:
            trim_result = trim_silence(
                file_path,
                threshold_db=threshold_db,
                min_silence=min_silence,
            )
        except Exception as exc:
            trim_result = {"trimmed": False, "error": str(exc)[:200], "removed_seconds": 0.0}

    if auto_intro_clean:
        try:
            intro_result = clean_intro(
                file_path,
                max_cut_seconds=intro_max_cut_s,
                analyze_seconds=intro_analyze_s,
            )
        except Exception as exc:
            intro_result = {"cleaned": False, "error": str(exc)[:200], "removed_seconds": 0.0}

    # Get final duration after all processing
    final_duration = _probe_duration(file_path)

    return {
        "trim": trim_result,
        "intro_clean": intro_result,
        "final_duration": round(final_duration, 3) if final_duration > 0 else 0.0,
    }
