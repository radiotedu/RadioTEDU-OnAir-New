import json

from app.dependency_bootstrap import (
    ManagedDependency,
    _dependencies,
    bootstrap_dependencies,
    managed_binary_path,
)


def test_default_dependencies_include_ffprobe_bundle_copy():
    deps = {dep.executable_name: dep for dep in _dependencies()}
    assert deps["ffmpeg.exe"].install_mode == "copy_from_bundle"
    assert deps["ffplay.exe"].install_mode == "copy_from_bundle"
    assert deps["ffprobe.exe"].install_mode == "copy_from_bundle"
    assert deps["ffprobe.exe"].validation_args == ("-version",)


def test_bootstrap_copies_bundled_ffmpeg_ffplay_and_ffprobe(tmp_path, monkeypatch):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "ffmpeg.exe").write_bytes(b"ffmpeg-binary")
    (bundle_dir / "ffplay.exe").write_bytes(b"ffplay-binary")
    (bundle_dir / "ffprobe.exe").write_bytes(b"ffprobe-binary")

    monkeypatch.setenv("CLEANROOM_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr("app.dependency_bootstrap._meipass_dir", lambda: bundle_dir)
    monkeypatch.setattr(
        "app.dependency_bootstrap._dependencies",
        lambda: (
            ManagedDependency("ffmpeg.exe", "copy_from_bundle"),
            ManagedDependency("ffplay.exe", "copy_from_bundle"),
            ManagedDependency("ffprobe.exe", "copy_from_bundle"),
        ),
    )
    monkeypatch.setattr(
        "app.dependency_bootstrap._validate_binary",
        lambda path, *_args, **_kwargs: path.exists(),
    )

    result = bootstrap_dependencies()

    assert result["ffmpeg.exe"]["installed"] is True
    assert result["ffplay.exe"]["installed"] is True
    assert result["ffprobe.exe"]["installed"] is True
    assert managed_binary_path("ffmpeg.exe").read_bytes() == b"ffmpeg-binary"
    assert managed_binary_path("ffplay.exe").read_bytes() == b"ffplay-binary"
    assert managed_binary_path("ffprobe.exe").read_bytes() == b"ffprobe-binary"


def test_bootstrap_downloads_ytdlp_into_managed_bin(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr("app.dependency_bootstrap._meipass_dir", lambda: None)
    monkeypatch.setattr(
        "app.dependency_bootstrap._dependencies",
        lambda: (
            ManagedDependency(
                "yt-dlp.exe",
                "download",
                download_url="https://example.com/yt-dlp.exe",
            ),
        ),
    )

    def _fake_download(url: str, destination):
        assert url == "https://example.com/yt-dlp.exe"
        destination.write_bytes(b"yt-dlp-binary")

    monkeypatch.setattr("app.dependency_bootstrap._download_to_path", _fake_download)
    monkeypatch.setattr(
        "app.dependency_bootstrap._validate_binary",
        lambda path, *_args, **_kwargs: path.exists(),
    )

    result = bootstrap_dependencies()

    assert result["yt-dlp.exe"]["installed"] is True
    assert managed_binary_path("yt-dlp.exe").read_bytes() == b"yt-dlp-binary"


def test_bootstrap_records_failure_without_raising(tmp_path, monkeypatch):
    tools_dir = tmp_path / "tools"
    monkeypatch.setenv("CLEANROOM_TOOLS_DIR", str(tools_dir))
    monkeypatch.setattr("app.dependency_bootstrap._meipass_dir", lambda: None)
    monkeypatch.setattr(
        "app.dependency_bootstrap._dependencies",
        lambda: (
            ManagedDependency(
                "yt-dlp.exe",
                "download",
                download_url="https://example.com/yt-dlp.exe",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.dependency_bootstrap._download_to_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = bootstrap_dependencies()

    assert result["yt-dlp.exe"]["installed"] is False
    assert result["yt-dlp.exe"]["status"] == "failed"
    assert "boom" in result["yt-dlp.exe"]["error"]

    state_path = tools_dir / "bootstrap-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["yt-dlp.exe"]["status"] == "failed"
    assert "boom" in payload["yt-dlp.exe"]["error"]


def test_bootstrap_keeps_valid_binary_when_windows_replacement_is_denied(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr("app.dependency_bootstrap._meipass_dir", lambda: None)
    monkeypatch.setattr(
        "app.dependency_bootstrap._dependencies",
        lambda: (
            ManagedDependency(
                "yt-dlp.exe",
                "download",
                download_url="https://example.com/yt-dlp.exe",
            ),
        ),
    )
    validation_results = iter((False, True))
    monkeypatch.setattr(
        "app.dependency_bootstrap._validate_binary",
        lambda *_args, **_kwargs: next(validation_results),
    )
    monkeypatch.setattr(
        "app.dependency_bootstrap._download_to_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("replacement denied")
        ),
    )

    result = bootstrap_dependencies()

    assert result["yt-dlp.exe"]["installed"] is True
    assert result["yt-dlp.exe"]["status"] == "ready"
    assert result["yt-dlp.exe"]["error"] == ""
