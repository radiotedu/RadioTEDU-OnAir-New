from app.audio.microphone_readiness import PhysicalMicrophoneReadiness


def test_readiness_is_unknown_and_nonblocking_until_background_refresh(monkeypatch):
    started = []

    class _Thread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            return None

    monkeypatch.setattr("app.audio.microphone_readiness.threading.Thread", _Thread)
    monitor = PhysicalMicrophoneReadiness(device_lister=lambda: ["Studio Mic"], enabled=True)

    snapshot = monitor.snapshot(live=False, receiving=False)

    assert snapshot["presence"] == "unknown"
    assert snapshot["selection"] == "unknown"
    assert snapshot["live"] == "idle"
    assert snapshot["receiving"] == "not-receiving"
    assert started and started[0]["daemon"] is True


def test_readiness_reports_present_selected_live_receiving_without_hardware_ids(monkeypatch):
    monkeypatch.setenv("CLEANROOM_PHYSICAL_MICROPHONE_LABEL", "Studio Microphone (USB Audio)")
    monitor = PhysicalMicrophoneReadiness(
        device_lister=lambda: ["@device_cm_{hardware-id}", "Studio Microphone (USB Audio)"], enabled=True
    )

    monitor._refresh()
    snapshot = monitor.snapshot(live=True, receiving=True)

    assert snapshot == {
        "presence": "present",
        "selection": "selected",
        "live": "live",
        "receiving": "receiving",
        "label": "",
        "refreshing": False,
        "discovery": "best_effort_passive",
        "observed_at": snapshot["observed_at"],
        "age_seconds": 0.0,
        "stale": False,
        "error_code": "",
    }


def test_readiness_reports_missing_or_unknown_without_leaking_errors():
    missing = PhysicalMicrophoneReadiness(device_lister=lambda: [], enabled=True)
    missing._refresh()
    assert missing.snapshot(live=False, receiving=False)["presence"] == "missing"

    unknown = PhysicalMicrophoneReadiness(device_lister=lambda: (_ for _ in ()).throw(RuntimeError("C:/secret")), enabled=True)
    unknown._refresh()
    snapshot = unknown.snapshot(live=False, receiving=False)
    assert snapshot["presence"] == "unknown"
    assert "secret" not in repr(snapshot)


def test_discovery_is_disabled_by_default_and_never_calls_ffmpeg():
    called = []
    monitor = PhysicalMicrophoneReadiness(device_lister=lambda: called.append(True) or ["Private Mic"])
    snapshot = monitor.snapshot(live=False, receiving=False)
    assert snapshot["presence"] == "disabled"
    assert snapshot["label"] == ""
    assert called == []


def test_readiness_distinguishes_ffmpeg_unavailable_and_timeout():
    unavailable = PhysicalMicrophoneReadiness(device_lister=lambda: (_ for _ in ()).throw(FileNotFoundError()), enabled=True)
    unavailable._refresh()
    assert unavailable.snapshot(live=False, receiving=False)["error_code"] == "ffmpeg_unavailable"
