from pathlib import Path


def test_readme_mentions_icecast_and_legacy_ui():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert "Icecast" in text
    assert "/ws?token=" in text
    assert "single supported product path" in text
    assert "On Air" in text
    assert "Playlists" in text
    assert "build_backend_onefile.ps1" in text
    assert "package_portable_release.ps1" in text
    assert "FFmpeg" in text
    assert "music -> music" in text
    assert "MediaRecorder" in text
    assert "/api/audio/live/settings" in text
    assert "WebRTC" in text
    assert "TURN" in text


def test_readme_mentions_live_mic_roles_and_browser_permissions():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()
    assert "admin" in lowered
    assert "dj" in lowered
    assert "microphone permission" in lowered
    assert "/ws?token=" in text


def test_readme_mentions_first_run_dependency_bootstrap():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower().replace("\\", "/")
    assert "first launch" in lowered
    assert "yt-dlp" in lowered
    assert "ffmpeg" in lowered
    assert "ffplay" in lowered
    assert "ffprobe" in lowered
    assert "%localappdata%/radiotedu onair/tools" in lowered
