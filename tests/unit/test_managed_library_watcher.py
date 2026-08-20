from app.services.managed_library_watcher import (
    ManagedLibraryProfile,
    ManagedLibraryWatcher,
)


def test_watcher_waits_for_stable_copy_then_syncs_supported_audio(tmp_path):
    folder = tmp_path / "Songs"
    folder.mkdir()
    # Long enough to catch fixed-size filename assumptions while keeping the
    # complete temporary path below legacy Windows MAX_PATH on CI hosts that
    # have not enabled long-path support.
    long_name = ("a" * 120) + ".mp3"
    (folder / long_name).write_bytes(b"audio")
    calls = []
    profile = ManagedLibraryProfile(
        station_id=7,
        track_type="music",
        folder=str(folder),
    )
    watcher = ManagedLibraryWatcher(
        profile_provider=lambda: [profile],
        sync_callback=lambda item: calls.append(item) or {"verified": True},
        required_stable_polls=2,
    )

    watcher.poll_once(now=1)
    assert calls == []
    assert watcher.snapshot()["profiles"][0]["status"] == "settling"

    watcher.poll_once(now=2)
    assert calls == [profile]
    assert watcher.snapshot()["profiles"][0]["status"] == "watching"

    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    watcher.poll_once(now=3)
    assert calls == [profile]


def test_watcher_detects_changed_audio_and_uses_bounded_retry(tmp_path):
    folder = tmp_path / "Jingles"
    folder.mkdir()
    media = folder / "id.ogg"
    media.write_bytes(b"first")
    attempts = []
    profile = ManagedLibraryProfile(
        station_id=1,
        track_type="jingle",
        folder=str(folder),
    )

    def failing_sync(item):
        attempts.append(item)
        raise RuntimeError("malformed media")

    watcher = ManagedLibraryWatcher(
        profile_provider=lambda: [profile],
        sync_callback=failing_sync,
        required_stable_polls=2,
        max_retries=1,
    )
    watcher.poll_once(now=1)
    watcher.poll_once(now=2)
    assert len(attempts) == 1
    assert watcher.snapshot()["profiles"][0]["status"] == "retry_wait"

    watcher.poll_once(now=2.5)
    assert len(attempts) == 1
    watcher.poll_once(now=3)
    assert len(attempts) == 2
    watcher.poll_once(now=10)
    assert len(attempts) == 2
    assert watcher.snapshot()["profiles"][0]["status"] == "failed"

    media.write_bytes(b"changed")
    watcher.poll_once(now=11)
    assert watcher.snapshot()["profiles"][0]["status"] == "settling"


def test_default_watcher_retries_sync_forever_with_capped_backoff(tmp_path):
    folder = tmp_path / "Pop"
    folder.mkdir()
    (folder / "track.flac").write_bytes(b"audio")
    attempts = []
    profile = ManagedLibraryProfile(
        station_id=4,
        track_type="music",
        folder=str(folder),
    )

    def failing_sync(item):
        attempts.append(item)
        raise RuntimeError("temporary disk or decoder failure")

    watcher = ManagedLibraryWatcher(
        profile_provider=lambda: [profile],
        sync_callback=failing_sync,
        required_stable_polls=2,
    )
    watcher.poll_once(now=1)
    for attempt in range(1, 10):
        watcher.poll_once(now=1 + (2 ** min(20, attempt)))

    snapshot = watcher.snapshot()["profiles"][0]
    assert len(attempts) >= 5
    assert snapshot["status"] == "retry_wait"
    assert snapshot["retry_forever"] is True


def test_watcher_periodically_rescans_stable_library(tmp_path):
    folder = tmp_path / "Lofi"
    folder.mkdir()
    (folder / "track.mp3").write_bytes(b"audio")
    calls = []
    profile = ManagedLibraryProfile(
        station_id=2,
        track_type="music",
        folder=str(folder),
        rescan_interval_seconds=600,
    )
    watcher = ManagedLibraryWatcher(
        profile_provider=lambda: [profile],
        sync_callback=lambda item: calls.append(item) or {"verified": True},
        required_stable_polls=2,
    )

    watcher.poll_once(now=1)
    watcher.poll_once(now=2)
    assert len(calls) == 1

    watcher.poll_once(now=602)
    assert len(calls) == 2


def test_watcher_treats_sidecar_artwork_as_a_library_change(tmp_path):
    folder = tmp_path / "Pop"
    folder.mkdir()
    (folder / "track.flac").write_bytes(b"audio")
    calls = []
    profile = ManagedLibraryProfile(
        station_id=4,
        track_type="music",
        folder=str(folder),
    )
    watcher = ManagedLibraryWatcher(
        profile_provider=lambda: [profile],
        sync_callback=lambda item: calls.append(item) or {"verified": True},
        required_stable_polls=2,
    )

    watcher.poll_once(now=1)
    watcher.poll_once(now=2)
    assert len(calls) == 1

    (folder / "cover.jpg").write_bytes(b"artwork")
    watcher.poll_once(now=3)
    assert len(calls) == 1
    watcher.poll_once(now=4)
    assert len(calls) == 2


def test_default_watcher_sync_is_incremental(tmp_path, monkeypatch):
    from app import db
    from app.api import legacy
    from app.engine import broadcast_queue_autofill
    from app.services import managed_library_watcher

    folder = tmp_path / "RadioTEDU"
    folder.mkdir()
    captured = []

    def fake_sync(payload):
        captured.append(payload)
        return {"verified": True}

    class FakeConnection:
        def __init__(self):
            self.committed = False
            self.closed = False

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(legacy, "sync_station_library_folder", fake_sync)
    connection = FakeConnection()
    reconciled = []
    monkeypatch.setattr(db, "get_connection", lambda: connection)
    monkeypatch.setattr(
        broadcast_queue_autofill,
        "reconcile_pending_sweeper_queue",
        lambda _conn, station_id: reconciled.append(station_id) or [],
    )
    profile = ManagedLibraryProfile(
        station_id=1,
        track_type="music",
        folder=str(folder),
    )
    result = managed_library_watcher._default_sync_callback(profile)

    assert result == {"verified": True, "queue_reconciled": True}
    assert len(captured) == 1
    assert captured[0].incremental is True
    assert captured[0].skip_unplayable is True
    assert captured[0].guard_configured_folder is True
    assert captured[0].allow_empty is True
    assert reconciled == [1]
    assert connection.committed is True
    assert connection.closed is True
