"""Real, station-owned local media for queue persistence tests."""
from pathlib import Path
import wave
from app.db import get_connection, init_db


def seed_track(root, station_id, track_id):
    init_db()
    path = Path(root) / f'station-{station_id}-track-{track_id}.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b'\x00\x01\x00\x01' * 48000)
    conn = get_connection()
    try:
        conn.execute('INSERT OR IGNORE INTO stations (id,name) VALUES (?,?)',
                     (station_id, f'Test station {station_id}'))
        conn.execute('INSERT OR IGNORE INTO tracks '
            '(id,station_id,title,artist,file_path,duration,is_active,track_type) '
            "VALUES (?,?,?,'Test',?,1,1,'music')",
            (track_id, station_id, f'Track {track_id}', str(path)))
        conn.commit()
    finally:
        conn.close()
