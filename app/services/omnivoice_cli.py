from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import soundfile as sf
import torch
from omnivoice import OmniVoice

DEFAULT_MODEL_ID = "k2-fsa/OmniVoice"
DEFAULT_SAMPLE_RATE = 24000


def _load_model(model_name: str):
    return OmniVoice.from_pretrained(
        str(model_name or DEFAULT_MODEL_ID),
        device_map="cpu",
        dtype=torch.float32,
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid_json:{exc}"}))
        return 1

    text = str(payload.get("text", "") or "").strip()
    output_path = Path(str(payload.get("output_path", "") or "")).expanduser()
    instruct = str(
        payload.get("instruct", "male, middle-aged, moderate pitch, american accent") or ""
    ).strip()
    model_name = str(payload.get("model_name", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID).strip()
    speed = float(payload.get("speed", 1.0) or 1.0)

    if not text:
        print(json.dumps({"ok": False, "error": "missing_text"}))
        return 1
    if not str(output_path):
        print(json.dumps({"ok": False, "error": "missing_output_path"}))
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    try:
        model = _load_model(model_name)
        audio = model.generate(
            text=text,
            instruct=instruct,
            speed=speed,
        )
        wave = audio[0].squeeze(0).detach().cpu().numpy()
        sf.write(str(output_path), wave, DEFAULT_SAMPLE_RATE)
        duration_seconds = float(len(wave) / DEFAULT_SAMPLE_RATE)
        print(
            json.dumps(
                {
                    "ok": True,
                    "output_path": str(output_path),
                    "duration_seconds": round(duration_seconds, 2),
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                    "sample_rate": DEFAULT_SAMPLE_RATE,
                }
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
