from pathlib import Path
import os

import app.runtime_paths as runtime_paths

NATIVE_BINARY = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'


def test_resolve_binary_from_meipass(tmp_path, monkeypatch):
    fake_bin = tmp_path / NATIVE_BINARY
    fake_bin.write_text("", encoding="utf-8")

    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(runtime_paths.shutil, "which", lambda _: None)
    monkeypatch.setattr(runtime_paths, "_managed_binary", lambda _: None)

    resolved = runtime_paths.resolve_binary("ffmpeg.exe")
    assert resolved == str(fake_bin)


def test_resolve_binary_from_path(monkeypatch):
    monkeypatch.delattr(runtime_paths.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(runtime_paths.shutil, "which", lambda name: f"C:/bin/{name}")
    monkeypatch.setattr(runtime_paths, "_managed_binary", lambda _: None)
    resolved = runtime_paths.resolve_binary("ffmpeg.exe")
    assert resolved == f"C:/bin/{NATIVE_BINARY}"


def test_resolve_binary_prefers_managed_tools_over_meipass(tmp_path, monkeypatch):
    managed_bin = tmp_path / "tools" / "bin"
    managed_bin.mkdir(parents=True)
    managed_binary = managed_bin / NATIVE_BINARY
    managed_binary.write_text("", encoding="utf-8")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled_binary = bundle / NATIVE_BINARY
    bundled_binary.write_text("", encoding="utf-8")

    monkeypatch.setenv("CLEANROOM_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(runtime_paths.shutil, "which", lambda name: f"C:/bin/{name}")

    resolved = runtime_paths.resolve_binary("ffmpeg.exe")
    assert resolved == str(managed_binary)
