from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_MODEL_DIR = r"C:\Program Files\RadioTEDU OnAir\_internal\models\qwen3-tts-0.6b-customvoice"
DEFAULT_SAMPLE_RATE = 24000

SPEAKER_MAP = {
    "warm_friend": "Aiden",
    "warm_radio_host": "Aiden",
    "classical_host": "Aiden",
    "energetic_host": "Ryan",
    "energetic_morning": "Ryan",
    "late_night_host": "Aiden",
    "professional_announcer": "Aiden",
    "smooth_evening": "Aiden",
    "casual_friend": "Ryan",
    "jazz_lounge": "Aiden",
    "news_bulletin": "Aiden",
    "default": "Aiden",
}


def _resolve_speaker(persona: str) -> str:
    return SPEAKER_MAP.get(persona.lower().strip(), "Aiden")


def _resolve_model_dir() -> str:
    return str(os.environ.get("QWEN_TTS_MODEL_DIR", "") or DEFAULT_MODEL_DIR)


def _resolve_threads() -> int:
    try:
        return max(1, min(4, int(os.environ.get("QWEN_TTS_THREADS", "2"))))
    except (TypeError, ValueError):
        return 2


def _resolve_dtype(torch):
    token = str(os.environ.get("QWEN_TTS_DTYPE", "float32") or "float32").lower()
    if token in {"float32", "fp32"}:
        return torch.float32
    if token in {"float16", "fp16", "half"}:
        return torch.float16
    return torch.bfloat16


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    import torch
    from qwen_tts import Qwen3TTSModel
    import soundfile as sf
    import numpy as np

    torch.set_num_threads(_resolve_threads())

    model_dir = _resolve_model_dir()
    model_name = Path(model_dir).name.lower()
    dtype = _resolve_dtype(torch)

    sys.stderr.write(f"Loading TTS model from {model_dir} ({dtype})...\n")
    sys.stderr.flush()
    load_start = time.monotonic()
    model = Qwen3TTSModel.from_pretrained(
        model_dir,
        device_map="cpu",
        dtype=dtype,
        low_cpu_mem_usage=True,
    )
    if _truthy_env("QWEN_TTS_INT8"):
        try:
            model.model = torch.quantization.quantize_dynamic(
                model.model,
                {torch.nn.Linear},
                dtype=torch.qint8,
            )
            sys.stderr.write("Applied dynamic int8 quantization.\n")
            sys.stderr.flush()
        except Exception as exc:
            sys.stderr.write(f"Dynamic int8 quantization skipped: {exc}\n")
            sys.stderr.flush()
    sys.stderr.write(f"Model ready in {round(time.monotonic()-load_start,1)}s. Listening for requests...\n")
    sys.stderr.flush()

    # Signal ready on stdout
    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"invalid_json:{exc}"}), flush=True)
            continue

        if payload.get("command") == "shutdown":
            print(json.dumps({"ok": True}), flush=True)
            break

        text = str(payload.get("text", "") or "").strip()
        output_path = Path(str(payload.get("output_path", "") or "")).expanduser()
        instruct = str(payload.get("instruct", "Warm, energetic radio host") or "").strip()
        language = str(payload.get("language", "English") or "").strip()
        persona = str(payload.get("persona", "default") or "").strip()

        if not text or not str(output_path):
            print(json.dumps({"ok": False, "error": "missing_text_or_output"}), flush=True)
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        speaker = _resolve_speaker(persona)

        start = time.monotonic()
        try:
            if "voice-design" in model_name:
                wavs, sr = model.generate_voice_design(
                    text=text,
                    instruct=instruct,
                    language=language,
                    max_new_tokens=256,
                    temperature=0.9,
                    top_p=1.0,
                    repetition_penalty=1.05,
                )
            else:
                wavs, sr = model.generate_custom_voice(
                    text=text,
                    language=language,
                    speaker=speaker,
                    instruct=instruct,
                )
            w = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
            sf.write(str(output_path), np.asarray(w), sr)
            duration = round(float(len(np.asarray(w)) / sr), 2)
            print(json.dumps({
                "ok": True,
                "output_path": str(output_path),
                "duration_seconds": duration,
                "synth_seconds": round(time.monotonic() - start, 2),
            }), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
