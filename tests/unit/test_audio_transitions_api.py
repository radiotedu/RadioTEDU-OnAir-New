from app.db import get_connection


def test_crossfade_save_reads_back_without_touching_output(client, admin_token_headers):
    conn = get_connection()
    before = [tuple(r) for r in conn.execute('SELECT * FROM station_outputs ORDER BY station_id')]
    conn.close()
    response = client.put('/api/audio/transitions/settings', headers=admin_token_headers, json={'station_id': 1, 'crossfade_seconds': 3.25})
    assert response.status_code == 200
    assert response.json()['crossfade_seconds'] == 3.25
    saved = client.get('/api/audio/transitions/settings?station_id=1', headers=admin_token_headers)
    assert saved.json()['crossfade_seconds'] == 3.25
    conn = get_connection()
    assert [tuple(r) for r in conn.execute('SELECT * FROM station_outputs ORDER BY station_id')] == before
    conn.close()


def test_crossfade_rejects_negative_and_excessive_values(client, admin_token_headers):
    for seconds in (-1, 11):
        result = client.put('/api/audio/transitions/settings', headers=admin_token_headers, json={'station_id': 1, 'crossfade_seconds': seconds})
        assert result.status_code == 422


def test_station_worker_reads_station_crossfade_before_system_default(client, admin_token_headers):
    from app.repositories.settings_repo import SettingsRepository
    from app.engine.station_worker import StationWorker
    conn = get_connection()
    try:
        repo = SettingsRepository(conn)
        repo.upsert_system({'default_crossfade_seconds': 5})
        repo.upsert_station(1, {'default_crossfade_seconds': 2.75})
        worker = StationWorker.__new__(StationWorker)
        worker.conn = conn
        worker.station_id = 1
        assert worker._default_crossfade_seconds() == 2.75
    finally:
        conn.close()
