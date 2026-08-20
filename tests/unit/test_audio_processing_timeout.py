from types import SimpleNamespace

import app.audio.audio_processing as audio_processing


def test_duration_probe_honors_caller_timeout(monkeypatch):
    captured = {}

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"12.5"}}',
        )

    monkeypatch.setattr(audio_processing, "_get_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(audio_processing.subprocess, "run", fake_run)

    duration = audio_processing.probe_duration(
        "C:/music/test.mp3",
        timeout_seconds=2.5,
    )

    assert duration == 12.5
    assert captured["timeout"] == 2.5
