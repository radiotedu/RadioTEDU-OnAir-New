import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, get_connection
from app.engine.station_worker import StationWorker
from app.auth.dependencies import user_is_allowed_for_request
from app.services.special_events import (
    CONFIG_KEY, STATE_KEY, active_window, active_programs, scan_folder,
    save_program, read_programs, process_special_event, _cache,
)


def plan(**changes):
    return dict(id='october', revision='r1', name='29 Ekim', mode='playlist',
                starts_on='2026-10-29', ends_on='2026-10-29', local_start='09:00',
                local_end='17:00', timezone='Europe/Istanbul', enabled=True,
                annual=False, priority=100, metadata_title='', tracks=[],
                repeat_playlist=True, order='ordered', start_policy='exact',
                crossfade_seconds=3, **changes)


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize('stamp,expected', [('2026-10-29T05:59:59', False),
    ('2026-10-29T06:00:00', True), ('2026-10-29T13:59:59', True),
    ('2026-10-29T14:00:00', False), ('2026-10-30T06:00:00', False)])
def test_exact_istanbul_window(stamp, expected):
    assert bool(active_window(plan(), at(stamp))) is expected


def test_annual_full_day_and_overnight():
    p=plan();p.update(annual=True, local_start='00:00', local_end='00:00')
    assert active_window(p,at('2029-10-29T10:00:00'))
    assert not active_window(p,at('2029-10-29T21:00:00'))
    p.update(local_start='23:00',local_end='02:00')
    assert active_window(p,at('2029-10-29T22:30:00'))
    assert not active_window(p,at('2029-10-29T23:00:00'))


def test_independent_metadata_window_and_priority():
    audio=plan(); audio['priority']=200
    meta=plan();meta.update(id='memorial',mode='metadata',local_start='00:00',
                           local_end='00:00',metadata_title='29 Ekim Cumhuriyet Bayramı')
    picked,title=active_programs([meta,audio],at('2026-10-29T07:00:00'))
    assert picked[0]['id']=='october' and title==meta['metadata_title']
    picked,title=active_programs([meta,audio],at('2026-10-29T18:00:00'))
    assert picked is None and title==meta['metadata_title']


def test_cross_year_annual_and_disabled():
    p=plan();p.update(annual=True,starts_on='2026-12-31',ends_on='2027-01-01')
    assert active_window(p,at('2030-01-01T07:00:00'))
    p['enabled']=False
    assert active_window(p,at('2030-01-01T07:00:00')) is None


def test_folder_selection_order_and_symlinks(tmp_path):
    (tmp_path/'b.mp3').write_bytes(b'audio'); (tmp_path/'a.wav').write_bytes(b'audio')
    (tmp_path/'ignore.txt').write_text('not audio')
    child=tmp_path/'nested';child.mkdir();(child/'c.flac').write_bytes(b'audio')
    assert [p['title'] for p in scan_folder(str(tmp_path),False)]==['a','b']
    assert len(scan_folder(str(tmp_path),True))==3
    with pytest.raises(ValueError):scan_folder('relative-path')


def test_durable_idempotent_plan_and_station_isolation(tmp_path):
    init_db();conn=get_connection()
    try:
        values=plan();values.pop('id');values['client_id']='abcd'
        first=save_program(conn,1,values); second=save_program(conn,1,values)
        assert first['id']==second['id'] and first['revision']==second['revision'] and len(read_programs(conn,1))==1
        assert read_programs(conn,2)==[]
    finally:conn.close()
    conn=get_connection()
    try:assert read_programs(conn,1)[0]['name']=='29 Ekim'
    finally:conn.close()


def worker_rig(conn, runtime):
    w=SimpleNamespace(conn=conn,station_id=1,runtime_registry=runtime)
    for name in ('_runtime_playback_alive','_same_runtime_uri','_runtime_source_finished_naturally','_runtime_source_is_draining'):
        fn=getattr(StationWorker,name)
        setattr(w,name,fn if name in ('_runtime_playback_alive','_same_runtime_uri') else lambda *args,fn=fn:fn(w,*args))
    w._complete_special_event_predecessor=Mock()
    w._start_runtime_station=runtime.start_station
    w._set_playout_state=Mock()
    return w


def test_playout_owns_pool_without_consuming_normal_queue_and_returns(tmp_path):
    init_db();conn=get_connection();audio=tmp_path/'holiday.wav';audio.write_bytes(b'fixture')
    cfg=plan();cfg['tracks']=[{'path':str(audio),'title':'Holiday','artist':'','duration':20}]
    save_program(conn,1,cfg,'')
    runtime=Mock();runtime.status.return_value={'running':False,'active_input_uri':''}
    runtime.is_process_running.return_value=False
    w=worker_rig(conn,runtime);_cache.clear()
    assert process_special_event(w,at('2026-10-29T07:00:00'))['source']=='special_event'
    assert runtime.start_station.call_args.args==(1,str(audio))
    # A fresh worker object resumes the existing source without restarting it.
    runtime.status.return_value={'program_running':True,'output_feed_active':True,'active_input_uri':str(audio)}
    assert process_special_event(worker_rig(conn,runtime),at('2026-10-29T07:00:02'))['reason']=='track_in_progress'
    assert runtime.start_station.call_count==1
    assert process_special_event(w,at('2026-10-29T14:00:00')) is None
    state=conn.execute('SELECT state_json FROM special_event_playout WHERE station_id=1').fetchone()
    assert json.loads(state[0])=={}
    conn.close()


def test_pool_eof_loops_with_guarded_producer_restart(tmp_path):
    init_db();conn=get_connection();file=tmp_path/'loop.wav';file.write_bytes(b'x')
    p=plan();p['tracks']=[{'path':str(file),'title':'Loop','duration':1}]
    save_program(conn,1,p);_cache.clear()
    rt=Mock();rt.status.return_value={'running':False};rt.is_process_running.return_value=False
    w=worker_rig(conn,rt);process_special_event(w,at('2026-10-29T07:00:00'))
    rt.status.return_value={'program_running':False,'producer_eof':True,'active_input_uri':str(file)}
    rt.is_process_running.return_value=True
    process_special_event(w,at('2026-10-29T07:00:02'))
    assert rt.start_station.call_count==2
    assert rt.start_station.call_args.kwargs['force_restart'] is True
    assert rt.start_station.call_args.kwargs['expected_active_input_uri']==str(file)
    conn.close()


def test_no_special_programme_has_no_writes_or_audio_changes():
    init_db();conn=get_connection();changes=[]
    conn.set_trace_callback(lambda sql: changes.append(sql) if sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) else None)
    rt=Mock();_cache.clear()
    for _ in range(5):assert process_special_event(worker_rig(conn,rt),at('2026-10-29T07:00:00')) is None
    assert not changes and not rt.start_station.called
    conn.close()


def test_api_readback_and_pause_after_file_removed(client,tmp_path):
    file=tmp_path/'song.wav';file.write_bytes(b'audio')
    payload=plan();payload.pop('id');payload.pop('revision')
    payload.update(source_folder=str(tmp_path),tracks=[{'path':str(file),'title':'Music'}],client_id='abc123')
    response=client.post('/api/stations/1/special-events',json=payload)
    assert response.status_code==200,response.text
    id=response.json()['program']['id']; assert id=='abc123'
    assert client.get('/api/stations/1/special-events').json()['programs'][0]['track_count']==1
    file.unlink();payload['enabled']=False
    assert client.put('/api/stations/1/special-events/'+id,json=payload).status_code==200
    assert client.delete('/api/stations/1/special-events/'+id).status_code==200


def test_event_media_permissions():
    for path in ('/api/stations/1/special-events','/api/special-event-media/upload'):
        assert user_is_allowed_for_request({'role':'admin'},path,'POST')
        assert user_is_allowed_for_request({'role':'producer'},path,'POST')
        assert not user_is_allowed_for_request({'role':'viewer'},path,'POST')


def test_metadata_override_restores_latest_title_without_restart():
    from app.engine.runtime_registry import StationRuntimeRegistry
    from app.audio.gst_pipeline import StationPipelineConfig
    rt=Mock();reg=StationRuntimeRegistry(runtime_factory=lambda sid:rt)
    reg._schedule_icecast_metadata_update=Mock()
    cfg=StationPipelineConfig(input_uri='song.wav',stream_title='Regular',stream_artist='Artist',
        icecast_host='127.0.0.1',icecast_port=8000,icecast_mount='/test',icecast_user='source',
        icecast_password='',local_output_enabled=False,output_device_id='')
    reg._last_metadata_cfg[1]=cfg
    reg.set_program_metadata_override(1,'10 Kasım')
    assert reg._last_metadata_cfg[1].stream_title=='10 Kasım'
    reg.set_program_metadata_override(1,None)
    assert reg._last_metadata_cfg[1].stream_title=='Regular'
    assert reg._last_metadata_cfg[1].stream_artist=='Artist'
    assert not rt.start.called


def test_normal_queue_and_other_station_remain_intact(tmp_path):
    init_db();conn=get_connection()
    conn.execute("INSERT INTO stations(id,name) VALUES (2,'Other')")
    ids=[]
    for sid in (1,2):
        track=conn.execute("INSERT INTO tracks(station_id,file_path,track_type,title) VALUES (?,?,'music','Regular')",(sid,str(tmp_path/'normal.wav'))).lastrowid
        q=conn.execute("INSERT INTO queue_items(station_id,track_id,position,status) VALUES (?,?,1,'playing')",(sid,track)).lastrowid
        ids.append(q)
    conn.commit()
    audio=tmp_path/'holiday.wav';audio.write_bytes(b'x')
    cfg=plan();cfg['tracks']=[{'path':str(audio),'title':'Holiday','duration':20}]
    save_program(conn,1,cfg);_cache.clear()
    runtime=Mock();runtime.status.return_value={'running':False};runtime.is_process_running.return_value=False
    process_special_event(worker_rig(conn,runtime),at('2026-10-29T07:00:00'))
    assert conn.execute('SELECT status FROM queue_items WHERE id=?',(ids[0],)).fetchone()[0]=='pending'
    assert conn.execute('SELECT status FROM queue_items WHERE id=?',(ids[1],)).fetchone()[0]=='playing'
    assert conn.execute('SELECT COUNT(*) FROM queue_items').fetchone()[0]==2
    conn.close()


def test_restart_resumes_persisted_special_position(tmp_path):
    init_db();conn=get_connection();audio=tmp_path/'holiday.wav';audio.write_bytes(b'x')
    cfg=plan();cfg['tracks']=[{'path':str(audio),'title':'Holiday','duration':20}]
    save_program(conn,1,cfg);_cache.clear()
    rt=Mock();rt.status.return_value={'running':False};rt.is_process_running.return_value=False
    process_special_event(worker_rig(conn,rt),at('2026-10-29T07:00:00'))
    process_special_event(worker_rig(conn,rt),at('2026-10-29T07:00:07'))
    assert rt.start_station.call_args.kwargs['start_offset_seconds']==7
    conn.close()


def test_boundary_start_leaves_current_source_and_queue_untouched(tmp_path):
    init_db();conn=get_connection();audio=tmp_path/'holiday.wav';audio.write_bytes(b'x')
    cfg=plan();cfg.update(start_policy='boundary',tracks=[{'path':str(audio),'title':'Holiday'}])
    save_program(conn,1,cfg);_cache.clear()
    rt=Mock();rt.status.return_value={'program_running':True,'active_input_uri':'regular.wav'}
    result=process_special_event(worker_rig(conn,rt),at('2026-10-29T07:00:00'))
    assert result['reason']=='special_event_waiting_for_boundary'
    assert not rt.start_station.called
    conn.close()


@pytest.mark.parametrize('drain_flag', [None, 'producer_draining', 'producer_finalizing'])
def test_nine_oclock_programme_waits_until_nine_oh_two_eof(tmp_path, drain_flag):
    init_db(); conn = get_connection()
    audio = tmp_path / 'holiday.wav'; audio.write_bytes(b'fixture')
    cfg = plan(); cfg.update(start_policy='boundary', tracks=[{'path': str(audio), 'title': 'Holiday'}])
    save_program(conn, 1, cfg); _cache.clear()
    rt = Mock(); rt.is_process_running.return_value = True
    rt.status.return_value = {'program_running': drain_flag is None, 'active_input_uri': 'regular.wav'}
    if drain_flag:
        rt.status.return_value[drain_flag] = True
    w = worker_rig(conn, rt)
    writes = conn.total_changes
    for clock in ('06:00:00', '06:01:59'):
        result = process_special_event(w, at('2026-10-29T' + clock))
        assert result['reason'] == 'special_event_waiting_for_boundary'
        assert not rt.start_station.called
        assert not w._complete_special_event_predecessor.called
        assert conn.total_changes == writes
    rt.status.return_value = {'program_running': False, 'producer_eof': True, 'active_input_uri': 'regular.wav'}
    result = process_special_event(w, at('2026-10-29T06:02:00'))
    assert result['source'] == 'special_event'
    assert rt.start_station.call_count == 1
    assert rt.start_station.call_args.kwargs['crossfade_seconds'] == 0
    assert rt.start_station.call_args.kwargs['start_offset_seconds'] == 0
    w._complete_special_event_predecessor.assert_called_once()
    conn.close()


def test_default_payload_waits_but_explicit_exact_is_retained():
    from app.api.special_events import EventPayload
    payload = dict(name='Programme', starts_on='2026-10-29', ends_on='2026-10-29')
    assert EventPayload(**payload).start_policy == 'boundary'
    assert EventPayload(**payload, start_policy='exact').start_policy == 'exact'


def test_explicit_exact_takeover_still_uses_saved_crossfade(tmp_path):
    init_db(); conn = get_connection()
    audio = tmp_path / 'holiday.wav'; audio.write_bytes(b'fixture')
    cfg = plan(); cfg.update(crossfade_seconds=4, tracks=[{'path': str(audio), 'title': 'Holiday'}])
    save_program(conn, 1, cfg); _cache.clear()
    rt = Mock(); rt.status.return_value = {'program_running': True, 'active_input_uri': 'regular.wav'}
    assert process_special_event(worker_rig(conn, rt), at('2026-10-29T06:00:00'))['source'] == 'special_event'
    assert rt.start_station.call_args.kwargs['crossfade_seconds'] == 4
    conn.close()


def test_boundary_completion_does_not_replay_finished_song_or_touch_other_station(tmp_path):
    init_db(); conn = get_connection()
    conn.execute("INSERT INTO stations(id,name) VALUES (2,'Other')")
    normal = tmp_path / 'normal.wav'; normal.write_bytes(b'fixture')
    ids = []
    for sid in (1, 2):
        tid = conn.execute("INSERT INTO tracks(station_id,file_path,track_type,title) VALUES (?,?,'music','Regular')", (sid, str(normal))).lastrowid
        ids.append(conn.execute("INSERT INTO queue_items(station_id,track_id,position,status,started_at) VALUES (?,?,1,'playing',CURRENT_TIMESTAMP)", (sid, tid)).lastrowid)
    conn.commit()
    audio = tmp_path / 'holiday.wav'; audio.write_bytes(b'fixture')
    cfg = plan(); cfg.update(start_policy='boundary', tracks=[{'path': str(audio), 'title': 'Holiday'}])
    save_program(conn, 1, cfg); _cache.clear()
    rt = Mock(); rt.is_process_running.return_value = True
    rt.status.return_value = {'program_running': False, 'producer_eof': True, 'active_input_uri': str(normal)}
    worker = StationWorker(1, runtime_registry=rt)
    worker._start_runtime_station = rt.start_station
    try:
        assert process_special_event(worker, at('2026-10-29T06:02:00'))['source'] == 'special_event'
        assert conn.execute('SELECT status FROM queue_items WHERE id=?', (ids[0],)).fetchone()[0] == 'done'
        assert conn.execute('SELECT status FROM queue_items WHERE id=?', (ids[1],)).fetchone()[0] == 'playing'
        assert conn.execute('SELECT play_count FROM tracks WHERE station_id=1').fetchone()[0] == 1
        assert rt.start_station.call_count == 1
    finally:
        worker.conn.close(); conn.close()


def test_uploaded_selection_preserves_exact_file_bytes(client,tmp_path,monkeypatch):
    monkeypatch.setattr('app.api.special_events.get_data_dir',lambda:tmp_path)
    data=b'RIFF'+bytes(range(256))*2
    response=client.post('/api/special-event-media/upload',files={'file':('song.wav',data,'audio/wav')})
    assert response.status_code==200,response.text
    from pathlib import Path
    imported=Path(response.json()['track']['path'])
    assert imported.read_bytes()==data
    assert not list(imported.parent.glob('*.part'))


def test_missing_pool_returns_to_continuity_with_bounded_retry(tmp_path):
    init_db();conn=get_connection();cfg=plan();cfg['tracks']=[{'path':str(tmp_path/'missing.wav'),'title':'Missing'}]
    save_program(conn,1,cfg);_cache.clear()
    rt=Mock();rt.status.return_value={'running':False};rt.is_process_running.return_value=False
    w=worker_rig(conn,rt)
    assert process_special_event(w,at('2026-10-29T07:00:00')) is None
    changes=conn.total_changes
    assert process_special_event(w,at('2026-10-29T07:00:01')) is None
    assert conn.total_changes==changes and not rt.start_station.called
    conn.close()


def test_source_startup_grace_does_not_respawn_the_decoder(tmp_path):
    init_db();conn=get_connection();file=tmp_path/'slow.wav';file.write_bytes(b'x')
    p=plan();p['tracks']=[{'path':str(file),'title':'Slow'}];save_program(conn,1,p);_cache.clear()
    rt=Mock();rt.status.return_value={'running':False};rt.is_process_running.return_value=False
    w=worker_rig(conn,rt);process_special_event(w,at('2026-10-29T07:00:00'))
    rt.status.return_value={'program_running':False,'active_input_uri':str(file),'producer_exit_code':None}
    result=process_special_event(w,at('2026-10-29T07:00:01'))
    assert result['reason']=='source_starting' and rt.start_station.call_count==1
    conn.close()


def test_large_plan_payload_never_enters_normal_station_settings(tmp_path):
    init_db();conn=get_connection();p=plan();p['tracks']=[{'path':str(tmp_path/'song.wav'),'title':'Song'}]
    save_program(conn,1,p)
    assert conn.execute('SELECT COUNT(*) FROM special_event_programs WHERE station_id=1').fetchone()[0]==1
    assert not conn.execute('SELECT 1 FROM station_settings WHERE key=?',(CONFIG_KEY,)).fetchone()
    conn.close()


def test_file_uri_identity_preserves_absolute_path_and_spaces(tmp_path):
    from app.services.special_events import _same_media
    file=tmp_path/'memorial song.wav'
    assert _same_media(str(file),file.as_uri())
