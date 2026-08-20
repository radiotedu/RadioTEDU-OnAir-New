from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


DEFAULT_YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DEPENDENCY_STATE_VERSION = 2
_WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


@dataclass(frozen=True)
class ManagedDependency:
    executable_name: str
    install_mode: str
    download_url: str = ""
    validation_args: tuple[str, ...] = ("--version",)


def _dependencies() -> tuple[ManagedDependency, ...]:
    return (
        ManagedDependency("ffmpeg.exe", "copy_from_bundle", validation_args=("-version",)),
        ManagedDependency("ffplay.exe", "copy_from_bundle", validation_args=("-version",)),
        ManagedDependency("ffprobe.exe", "copy_from_bundle", validation_args=("-version",)),
        ManagedDependency(
            "yt-dlp.exe",
            "download",
            download_url=os.getenv("CLEANROOM_YTDLP_URL", DEFAULT_YTDLP_URL).strip()
            or DEFAULT_YTDLP_URL,
        ),
    )


def _meipass_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    path = Path(str(base))
    return path if path.exists() else None


def _packaged_base_dirs() -> list[Path]:
    candidates: list[Path] = []
    bundle_dir = _meipass_dir()
    if bundle_dir is not None:
        candidates.append(bundle_dir)

    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass

    try:
        candidates.append(Path(__file__).resolve().parents[1])
    except Exception:
        pass

    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def managed_tools_dir() -> Path:
    raw = os.getenv("CLEANROOM_TOOLS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # A frozen deployment must use the tools shipped with the signed bundle.
        # Do not silently substitute writable per-database binaries.
        return (Path(sys.executable).resolve().parent / "tools").resolve()
    return (Path(__file__).resolve().parents[1] / "tools").resolve()


def managed_runtime_dir(name: str) -> Path:
    return managed_tools_dir() / "runtimes" / str(name)


def managed_bin_dir() -> Path:
    return managed_tools_dir() / "bin"


def managed_binary_path(executable_name: str) -> Path:
    return managed_bin_dir() / str(executable_name)


def bootstrap_state_path() -> Path:
    configured = os.getenv("CLEANROOM_DEPENDENCY_STATE_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # Keep explicit development/test tool sandboxes self-contained. Packaged
    # builds never write state beside Program Files binaries.
    if os.getenv("CLEANROOM_TOOLS_DIR", "").strip() and not getattr(
        sys, "frozen", False
    ):
        return managed_tools_dir() / "bootstrap-state.json"
    from app.config import get_data_root

    return get_data_root() / "state" / "dependency-bootstrap.json"


def _load_state() -> dict[str, dict]:
    path = bootstrap_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_bootstrap_state() -> dict[str, dict]:
    return _load_state()


def _write_text_atomic(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(destination)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _save_state(payload: dict[str, dict]) -> None:
    _write_text_atomic(
        bootstrap_state_path(),
        json.dumps(payload, indent=2, sort_keys=True),
    )


def _detect_webview2_runtime() -> dict[str, object]:
    if sys.platform != "win32":
        return {
            "path": "",
            "installed": False,
            "status": "unsupported",
            "error": "",
            "install_mode": "external_installer",
            "managed_path": str(managed_runtime_dir("webview2")),
            "version": "",
            "state_version": DEPENDENCY_STATE_VERSION,
            "updated_at": _now_iso(),
        }

    try:
        import winreg
    except Exception as exc:
        return {
            "path": "",
            "installed": False,
            "status": "failed",
            "error": str(exc),
            "install_mode": "external_installer",
            "managed_path": str(managed_runtime_dir("webview2")),
            "version": "",
            "state_version": DEPENDENCY_STATE_VERSION,
            "updated_at": _now_iso(),
        }

    registry_paths: list[tuple[object, str]] = [
        (winreg.HKEY_CURRENT_USER, fr"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_ID}"),
    ]
    if "PROGRAMFILES(X86)" in os.environ or "PROGRAMW6432" in os.environ:
        registry_paths.insert(
            0,
            (winreg.HKEY_LOCAL_MACHINE, fr"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_ID}"),
        )
    else:
        registry_paths.insert(
            0,
            (winreg.HKEY_LOCAL_MACHINE, fr"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_ID}"),
        )

    version = ""
    for hive, subkey in registry_paths:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if str(version or "").strip():
            break

    return {
        "path": "",
        "installed": bool(str(version or "").strip()),
        "status": "ready" if str(version or "").strip() else "missing",
        "error": "",
        "install_mode": "external_installer",
        "managed_path": str(managed_runtime_dir("webview2")),
        "version": str(version or ""),
        "state_version": DEPENDENCY_STATE_VERSION,
        "updated_at": _now_iso(),
    }


def _binary_candidates(name: str) -> list[Path]:
    target = managed_binary_path(name)
    candidate_paths = [target]
    system_path = shutil.which(name)
    if system_path:
        candidate_paths.append(Path(system_path))
    if name.lower() == "ollama.exe":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidate_paths.append(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe")
        program_files = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", "")]
        for root in program_files:
            if str(root or "").strip():
                candidate_paths.append(Path(str(root)) / "Ollama" / "ollama.exe")
    return candidate_paths


def _detect_binary_dependency(name: str, *, install_mode: str = "detect_only") -> dict[str, object]:
    target = managed_binary_path(name)
    candidate_paths = _binary_candidates(name)
    found_path = next((path for path in candidate_paths if path.exists()), None)
    return {
        "path": str(found_path or ""),
        "installed": found_path is not None,
        "status": "ready" if found_path is not None else "missing",
        "error": "",
        "install_mode": install_mode,
        "managed_path": str(target),
        "version": "",
        "state_version": DEPENDENCY_STATE_VERSION,
        "updated_at": _now_iso(),
    }


def _detect_managed_python_runtime() -> dict[str, object]:
    managed_dir = managed_runtime_dir("python")
    candidates = [
        managed_dir / "python.exe",
        managed_dir / "Scripts" / "python.exe",
    ]
    found = next((path for path in candidates if path.exists()), None)
    return {
        "path": str(found or ""),
        "installed": found is not None,
        "status": "ready" if found is not None else "missing",
        "error": "",
        "install_mode": "managed_runtime",
        "managed_path": str(managed_dir),
        "version": "",
        "state_version": DEPENDENCY_STATE_VERSION,
        "updated_at": _now_iso(),
    }


def _detect_qwen_tts_runtime() -> dict[str, object]:
    from app.services.ai_host import DEFAULT_TTS_LOCAL_DIR

    managed_dir = managed_runtime_dir("qwen-tts")
    candidates = [
        managed_dir,
        DEFAULT_TTS_LOCAL_DIR,
    ]
    found = next((path for path in candidates if path.exists()), None)
    return {
        "path": str(found or ""),
        "installed": found is not None,
        "status": "ready" if found is not None else "missing",
        "error": "",
        "install_mode": "managed_runtime",
        "managed_path": str(managed_dir),
        "version": "",
        "state_version": DEPENDENCY_STATE_VERSION,
        "updated_at": _now_iso(),
    }


def _failed_detection_entry(name: str, error: Exception) -> dict[str, object]:
    return {
        "path": "",
        "installed": False,
        "status": "failed",
        "error": str(error),
        "install_mode": "detect_only",
        "managed_path": "",
        "version": "",
        "state_version": DEPENDENCY_STATE_VERSION,
        "updated_at": _now_iso(),
    }


def _safe_detect(name: str, detector) -> dict[str, object]:
    try:
        return dict(detector())
    except Exception as exc:
        return _failed_detection_entry(name, exc)


def _auxiliary_dependency_state() -> dict[str, dict[str, object]]:
    return {
        "webview2-runtime": _safe_detect("webview2-runtime", _detect_webview2_runtime),
        "ollama.exe": _safe_detect("ollama.exe", lambda: _detect_binary_dependency("ollama.exe")),
        "python-runtime": _safe_detect("python-runtime", _detect_managed_python_runtime),
        "qwen-tts-runtime": _safe_detect("qwen-tts-runtime", _detect_qwen_tts_runtime),
    }


def _entry_with_error(base: dict[str, object], status: str, error: str) -> dict[str, object]:
    entry = dict(base)
    entry["installed"] = False
    entry["status"] = status
    entry["error"] = error
    entry["updated_at"] = _now_iso()
    return entry


def _resolve_packaged_file(env_name: str, *bundle_names: str) -> Path | None:
    raw = os.getenv(env_name, "").strip()
    if raw:
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists():
            return candidate
    for base_dir in _packaged_base_dirs():
        for name in bundle_names:
            candidate = base_dir / name
            if candidate.exists():
                return candidate
    return None


def _replace_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    try:
        shutil.copytree(source, tmp_path)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(tmp_path), str(destination))
    finally:
        if tmp_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)


def _extract_zip_to_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    try:
        tmp_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            root = tmp_path.resolve()
            for member in archive.infolist():
                target = (root / member.filename).resolve()
                if root != target and root not in target.parents:
                    raise ValueError(f"unsafe path in packaged runtime archive: {member.filename}")
            archive.extractall(tmp_path)
        children = [child for child in tmp_path.iterdir()]
        root = tmp_path
        if len(children) == 1 and children[0].is_dir():
            root = children[0]
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(root), str(destination))
    finally:
        if tmp_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)


def _install_managed_python_runtime() -> dict[str, object]:
    detected = _detect_managed_python_runtime()
    if bool(detected.get("installed")):
        return detected

    package = _resolve_packaged_file(
        "RADIOTEDU_MANAGED_PYTHON_ZIP",
        "prerequisites/python-embed-amd64.zip",
        "prerequisites/runtimes/python/python-embed.zip",
        "runtimes/python/python-embed.zip",
        "python-embed-amd64.zip",
    )
    if package is None:
        return detected

    try:
        _extract_zip_to_tree(package, managed_runtime_dir("python"))
    except Exception as exc:
        return _entry_with_error(detected, "failed", str(exc))

    installed = _detect_managed_python_runtime()
    if bool(installed.get("installed")):
        installed["status"] = "installed"
        return installed
    return _entry_with_error(installed, "failed", "packaged Python runtime did not validate after extraction")


def _install_qwen_tts_runtime() -> dict[str, object]:
    detected = _detect_qwen_tts_runtime()
    if bool(detected.get("installed")):
        return detected

    destination = managed_runtime_dir("qwen-tts")
    package_dir = _resolve_packaged_file(
        "RADIOTEDU_QWEN_TTS_DIR",
        "prerequisites/models/qwen3-tts-voice-design",
        "prerequisites/runtimes/qwen-tts",
        "models/qwen3-tts-voice-design",
        "runtimes/qwen-tts",
    )
    package_zip = _resolve_packaged_file(
        "RADIOTEDU_QWEN_TTS_ZIP",
        "prerequisites/models/qwen3-tts-voice-design.zip",
        "prerequisites/qwen3-tts-voice-design.zip",
        "models/qwen3-tts-voice-design.zip",
        "qwen3-tts-voice-design.zip",
    )

    try:
        if package_dir is not None and package_dir.is_dir():
            _replace_tree(package_dir, destination)
        elif package_zip is not None and package_zip.is_file():
            _extract_zip_to_tree(package_zip, destination)
        else:
            return detected
    except Exception as exc:
        return _entry_with_error(detected, "failed", str(exc))

    installed = _detect_qwen_tts_runtime()
    if bool(installed.get("installed")):
        installed["status"] = "installed"
        return installed
    return _entry_with_error(installed, "failed", "packaged Qwen TTS assets did not validate after extraction")


def _install_ollama_runtime() -> dict[str, object]:
    detected = _detect_binary_dependency("ollama.exe", install_mode="managed_or_external")
    if bool(detected.get("installed")):
        return detected

    package = _resolve_packaged_file(
        "RADIOTEDU_OLLAMA_EXE",
        "prerequisites/ollama.exe",
        "runtimes/ollama/ollama.exe",
        "ollama.exe",
    )
    if package is None or not package.is_file():
        return detected

    try:
        _copy_file_atomic(package, managed_binary_path("ollama.exe"))
    except Exception as exc:
        return _entry_with_error(detected, "failed", str(exc))

    installed = _detect_binary_dependency("ollama.exe", install_mode="managed_or_external")
    if bool(installed.get("installed")):
        installed["status"] = "installed"
        return installed
    return _entry_with_error(installed, "failed", "packaged Ollama executable did not validate after copy")


def _install_auxiliary_dependency_state() -> dict[str, dict[str, object]]:
    state = _auxiliary_dependency_state()
    state["ollama.exe"] = _install_ollama_runtime()
    state["python-runtime"] = _install_managed_python_runtime()
    state["qwen-tts-runtime"] = _install_qwen_tts_runtime()
    return state


def refresh_dependency_state() -> dict[str, dict]:
    state = _load_state()
    state.update(_auxiliary_dependency_state())
    try:
        _save_state(state)
    except Exception:
        pass
    return state


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copyfile(source, tmp_path)
        tmp_path.replace(destination)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _download_to_path(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(destination.name + ".tmp")
    try:
        with urlopen(url, timeout=30) as response, tmp_path.open("wb") as fh:
            shutil.copyfileobj(response, fh)
        tmp_path.replace(destination)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _validate_binary(path: Path, validation_args: tuple[str, ...] = ("--version",)) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if not validation_args:
        return True
    try:
        proc = subprocess.run(
            [str(path), *validation_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0


def bootstrap_dependencies() -> dict[str, dict]:
    dependencies = _dependencies()
    current_state: dict[str, dict] = {}
    try:
        managed_bin_dir().mkdir(parents=True, exist_ok=True)
        previous = _load_state()

        for dependency in dependencies:
            target = managed_binary_path(dependency.executable_name)
            entry = {
                "path": str(target),
                "installed": False,
                "status": "missing",
                "error": "",
                "install_mode": dependency.install_mode,
                "managed_path": str(target),
                "version": "",
                "state_version": DEPENDENCY_STATE_VERSION,
                "updated_at": _now_iso(),
            }

            try:
                if _validate_binary(target, dependency.validation_args):
                    entry["status"] = "ready"
                else:
                    if dependency.install_mode == "copy_from_bundle":
                        bundle_dir = _meipass_dir()
                        source = None
                        if bundle_dir is not None:
                            candidate = bundle_dir / dependency.executable_name
                            if candidate.exists():
                                source = candidate
                        # Fallback: find binary in system PATH
                        if source is None:
                            system_path = shutil.which(dependency.executable_name)
                            if system_path:
                                source = Path(system_path)
                        if source is None:
                            # Not fatal — binary may still be in PATH at runtime
                            entry["status"] = "skipped"
                            entry["error"] = f"not bundled, will use system PATH for {dependency.executable_name}"
                            current_state[dependency.executable_name] = entry
                            continue
                        _copy_file_atomic(source, target)
                    elif dependency.install_mode == "download":
                        if not dependency.download_url:
                            raise ValueError(
                                f"download url missing for {dependency.executable_name}"
                            )
                        _download_to_path(dependency.download_url, target)
                    else:
                        raise ValueError(
                            f"unsupported install mode: {dependency.install_mode}"
                        )

                    if not _validate_binary(target, dependency.validation_args):
                        raise RuntimeError(
                            f"validation failed for {dependency.executable_name}"
                        )
                    entry["installed"] = True
                    entry["status"] = "installed"
            except Exception as exc:
                # Windows can transiently deny replacement while another
                # process or security scanner still has the managed binary
                # open. Preserve a valid existing executable instead of
                # turning a harmless update race into a startup failure.
                if _validate_binary(target, dependency.validation_args):
                    entry["installed"] = True
                    entry["status"] = "ready"
                    entry["error"] = ""
                else:
                    entry["status"] = "failed"
                    entry["error"] = str(exc)

            current_state[dependency.executable_name] = entry

        previous.update(current_state)
        previous.update(_install_auxiliary_dependency_state())
        _save_state(previous)
        current_state.update(
            {key: previous[key] for key in previous if key not in current_state}
        )
    except Exception as exc:
        message = str(exc)
        for dependency in dependencies:
            current_state.setdefault(
                dependency.executable_name,
                {
                    "path": str(managed_binary_path(dependency.executable_name)),
                    "installed": False,
                    "status": "failed",
                    "error": message,
                    "install_mode": dependency.install_mode,
                    "managed_path": str(managed_binary_path(dependency.executable_name)),
                    "version": "",
                    "state_version": DEPENDENCY_STATE_VERSION,
                    "updated_at": _now_iso(),
                },
            )
        try:
            previous = _load_state()
            previous.update(current_state)
            previous.update(_install_auxiliary_dependency_state())
            _save_state(previous)
        except Exception:
            pass
    return current_state
