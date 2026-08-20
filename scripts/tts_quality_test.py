"""
TTS Quality Test Script

Tests the Qwen TTS voice quality with different presets and text samples.
Run this to evaluate and tune voice settings before deploying to production.

Usage:
    python scripts/tts_quality_test.py
    python scripts/tts_quality_test.py --preset warm_radio_host
    python scripts/tts_quality_test.py --list-presets
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def list_presets():
    """List all available voice presets."""
    from app.services.voice_presets import list_presets as _list

    presets = _list()
    print(f"\n{'='*60}")
    print(f"{'Voice Presets':^60}")
    print(f"{'='*60}")
    for p in presets:
        print(f"  {p['icon']} {p['preset_id']:<25} {p['display_name']}")
        print(f"     {p['description']}")
    print(f"{'='*60}\n")
    return presets


def load_tts_model(model_path: str = ""):
    """Load the TTS model and return it."""
    print("Loading TTS model...")
    try:
        from qwen_tts import Qwen3TTSModel

        from app.services.ai_host import DEFAULT_TTS_LOCAL_DIR

        path = model_path or str(DEFAULT_TTS_LOCAL_DIR)
        print(f"  Model path: {path}")

        model = Qwen3TTSModel.from_pretrained(
            path,
            device_map="cpu",
        )
        print("  Model loaded successfully!")
        return model
    except ImportError:
        print("  ERROR: qwen_tts not installed!")
        print("  Install with: pip install qwen-tts")
        return None
    except Exception as exc:
        print(f"  ERROR: Model load failed: {exc}")
        return None


def test_tts(
    model,
    text: str,
    preset_id: str,
    output_dir: Path,
    *,
    verbose: bool = False,
) -> dict:
    """Run a single TTS test and save the output."""
    from app.services.voice_presets import get_instruct_prompt
    from app.services.voice_enhancer import enhance_for_tts

    instruct = get_instruct_prompt(preset_id)
    enhanced = enhance_for_tts(text)

    if verbose:
        print(f"\n  Original text:  {text}")
        print(f"  Enhanced text:  {enhanced}")
        print(f"  Instruct prompt: {instruct[:100]}...")

    output_path = output_dir / f"tts_test_{preset_id}.wav"

    start = time.monotonic()
    try:
        wavs, sample_rate = model.generate_voice_design(
            text=enhanced,
            instruct=instruct,
            language="English",
            max_new_tokens=4096,
            temperature=0.85,
            top_p=0.95,
            repetition_penalty=1.1,
        )

        elapsed = round((time.monotonic() - start) * 1000, 2)

        if wavs and len(wavs) > 0:
            import numpy as np
            import soundfile as sf

            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output_path), np.concatenate(wavs), sample_rate)

            from app.audio.audio_processing import probe_duration
            duration = probe_duration(str(output_path))

            file_size = output_path.stat().st_size

            print(f"  Preset: {preset_id}")
            print(f"  Time: {elapsed}ms")
            print(f"  Duration: {duration:.2f}s")
            print(f"  File size: {file_size / 1024:.1f}KB")
            print(f"  Output: {output_path}")

            return {
                "preset": preset_id,
                "success": True,
                "time_ms": elapsed,
                "duration_s": round(duration, 2) if duration else 0,
                "file_size_kb": round(file_size / 1024, 1),
                "output_path": str(output_path),
            }
        else:
            print(f"  Preset: {preset_id} - FAILED (no audio)")
            return {"preset": preset_id, "success": False, "error": "No audio generated"}

    except Exception as exc:
        print(f"  Preset: {preset_id} - ERROR: {exc}")
        return {"preset": preset_id, "success": False, "error": str(exc)}


def run_all_tests(model, presets, output_dir: Path, verbose: bool = False):
    """Run tests for all presets."""
    test_texts = [
        (
            "You're listening to Radio TEDU. "
            "Up next is a beautiful piece by Mozart, "
            "performed by the Vienna Philharmonic Orchestra."
        ),
        (
            "This is Radio TEDU, live on air. "
            "Stay tuned for more great classical music."
        ),
        (
            "Welcome back to Radio TEDU. "
            "The piece you just heard was composed in 1785, "
            "during Mozart's most productive period."
        ),
    ]

    results = []

    for text_idx, text in enumerate(test_texts):
        print(f"\n{'='*60}")
        print(f"Test Text {text_idx + 1}: {text[:80]}...")
        print(f"{'='*60}")

        for preset in presets:
            preset_id = preset["preset_id"]
            result = test_tts(model, text, preset_id, output_dir, verbose=verbose)
            result["text_idx"] = text_idx
            results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print(f"{'Test Summary':^60}")
    print(f"{'='*60}")
    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count
    avg_time = (
        round(sum(r["time_ms"] for r in results if r.get("success")) / max(success_count, 1), 2)
        if success_count > 0
        else "N/A"
    )

    print(f"  Total tests: {len(results)}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    print(f"  Average generation time: {avg_time}ms")
    print(f"  Output directory: {output_dir}")
    print(f"{'='*60}\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="TTS Quality Test Script")
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        help="Test a specific preset only (e.g., warm_radio_host)",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List all available presets and exit",
    )
    parser.add_argument(
        "--model-path",
        type=str,
        default="",
        help="Path to TTS model directory",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(PROJECT_ROOT / "data" / "ai_cache" / "test_output"),
        help="Directory to save test output files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed output including prompts and text transformations",
    )
    parser.add_argument(
        "--text",
        type=str,
        default=None,
        help="Custom text to test (overrides default test texts)",
    )

    args = parser.parse_args()

    if args.list_presets:
        list_presets()
        return

    # Load model
    model = load_tts_model(args.model_path)
    if model is None:
        print("Aborting: Could not load TTS model.")
        sys.exit(1)

    # Get presets
    if args.preset:
        from app.services.voice_presets import get_preset
        preset_obj = get_preset(args.preset)
        if preset_obj is None:
            print(f"ERROR: Preset '{args.preset}' not found.")
            sys.exit(1)
        presets = [
            {
                "preset_id": preset_obj.preset_id,
                "display_name": preset_obj.display_name,
                "description": preset_obj.description,
            }
        ]
    else:
        presets = list_presets()

    # Run tests
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.text:
        # Single custom text test
        print(f"\nTesting custom text: {args.text}\n")
        for preset in presets:
            test_tts(model, args.text, preset["preset_id"], output_dir, verbose=args.verbose)
    else:
        run_all_tests(model, presets, output_dir, verbose=args.verbose)


if __name__ == "__main__":
    main()
