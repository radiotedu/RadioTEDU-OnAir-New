from pathlib import Path

import pytest

from app.services.managed_library_watcher import ManagedLibraryProfile, ManagedLibraryWatcher


def test_unreadable_subdirectory_does_not_publish_a_partial_inventory(tmp_path, monkeypatch):
    (tmp_path / 'one.mp3').write_bytes(b'audio')
    profile = ManagedLibraryProfile(station_id=4, track_type='music', folder=str(tmp_path), mode='replace')
    synced = []
    watcher = ManagedLibraryWatcher(profile_provider=lambda: [profile], sync_callback=lambda p: synced.append(p) or {})
    watcher.poll_once(now=10)
    watcher.poll_once(now=15)
    assert len(synced) == 1
    def broken_walk(*args, onerror, **kwargs):
        yield str(tmp_path), [], ['one.mp3']
        onerror(PermissionError('folder temporarily inaccessible'))
    monkeypatch.setattr('app.services.managed_library_watcher.os.walk', broken_walk)
    watcher.poll_once(now=20)
    watcher.poll_once(now=25)
    assert len(synced) == 1
    assert watcher.snapshot()['profiles'][0]['status'] == 'error'


def test_trash_is_not_a_live_music_folder(tmp_path):
    trash = tmp_path / '.radiotedu-trash'
    trash.mkdir()
    (trash / 'retired.mp3').write_bytes(b'retired')
    (tmp_path / 'current.mp3').write_bytes(b'current')
    profile = ManagedLibraryProfile(station_id=4, track_type='music', folder=str(tmp_path))
    _, count = ManagedLibraryWatcher._fingerprint(profile)
    assert count == 1


def test_posix_inventory_does_not_merge_different_filename_case(tmp_path, monkeypatch):
    from app.api import legacy
    monkeypatch.setattr(legacy.sys, 'platform', 'linux')
    assert legacy._canonical_library_path(tmp_path / 'Track.mp3') != legacy._canonical_library_path(tmp_path / 'track.mp3')


def test_windows_inventory_keeps_case_insensitive_path_identity(tmp_path, monkeypatch):
    from app.api import legacy
    monkeypatch.setattr(legacy.sys, 'platform', 'win32')
    assert legacy._canonical_library_path(tmp_path / 'Track.mp3') == legacy._canonical_library_path(tmp_path / 'track.mp3')
