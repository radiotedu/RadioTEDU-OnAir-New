from pathlib import Path
from app.db import get_connection
from app.api.legacy import LibraryFolderSyncPayload, sync_station_library_folder
from app.services.managed_library_watcher import ManagedLibraryProfile, _default_sync_callback


def test_live_folder_appends_new_music_once_and_removes_only_future_retired_music(client, tmp_path, monkeypatch):
    monkeypatch.setattr('app.api.legacy._get_audio_metadata', lambda p: {'title': Path(p).stem, 'duration': 120, 'artist': '', 'album': ''})
    folder = tmp_path / 'music'
    folder.mkdir()
    current = folder / 'Current.mp3'
    retired = folder / 'Retired.mp3'
    current.write_bytes(b'current')
    retired.write_bytes(b'retired')
    sync_station_library_folder(LibraryFolderSyncPayload(station_id=1, folder=str(folder), mode='replace'))
    conn = get_connection()
    ids = {row['title']: row['id'] for row in conn.execute('SELECT id, title FROM tracks WHERE station_id=1')}
    conn.execute("INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 1, 'playing')", (ids['Current'],))
    conn.execute("INSERT INTO queue_items (station_id, track_id, position, status) VALUES (1, ?, 2, 'pending')", (ids['Retired'],))
    conn.commit()
    conn.close()
    retired.unlink()
    (folder / 'New.mp3').write_bytes(b'new')
    profile = ManagedLibraryProfile(station_id=1, track_type='music', folder=str(folder), mode='replace')
    result = _default_sync_callback(profile)
    assert result['pending_queue_items_added'] == 1
    assert result['pending_queue_items_removed'] == 1
    assert _default_sync_callback(profile)['no_op']
    conn = get_connection()
    try:
        rows = conn.execute("SELECT t.title, q.status FROM queue_items q JOIN tracks t ON t.id=q.track_id WHERE q.station_id=1 AND q.status IN ('playing','pending') ORDER BY q.position").fetchall()
        assert [(r['title'], r['status']) for r in rows] == [('Current', 'playing'), ('New', 'pending')]
    finally:
        conn.close()
