from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVER_SCRIPT = str(Path(__file__).parent / "qwen_tts_server.py")
PYTHON_BIN = sys.executable

_server_proc: subprocess.Popen | None = None
_server_ready = False


def _ensure_server() -> subprocess.Popen | None:
    global _server_proc, _server_ready
    if _server_proc is not None and _server_proc.poll() is None and _server_ready:
        return _server_proc

    _server_ready = False
    try:
        _server_proc = subprocess.Popen(
            [PYTHON_BIN, SERVER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in _server_proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("ready"):
                _server_ready = True
                return _server_proc
        _server_proc = None
        return None
    except Exception:
        _server_proc = None
        return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid_json:{exc}"}))
        return 1

    model_dir = str(payload.get("model_dir", "") or "").strip()
    if model_dir:
        os.environ["QWEN_TTS_MODEL_DIR"] = model_dir

    server = _ensure_server()
    if server is None or server.poll() is not None:
        print(json.dumps({"ok": False, "error": "tts_server_not_running"}))
        return 1

    try:
        server.stdin.write(json.dumps(payload) + "\n")
        server.stdin.flush()
        for line in server.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(json.dumps(result))
            return 0 if result.get("ok") else 1
        print(json.dumps({"ok": False, "error": "server_closed_connection"}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        try:
            if server is not None and server.poll() is None and server.stdin is not None:
                server.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                server.stdin.flush()
        except Exception:
            pass
        try:
            if server is not None and server.poll() is None:
                server.wait(timeout=5)
        except Exception:
            try:
                server.terminate()
                server.wait(timeout=3)
            except Exception:
                try:
                    server.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
