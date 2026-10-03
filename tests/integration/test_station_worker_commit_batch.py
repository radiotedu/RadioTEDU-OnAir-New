import sqlite3
from types import SimpleNamespace

import pytest

from app.db import init_db
from app.db_commit_batch import CommitBatchAborted
from app.engine.playout_state import PlayoutStateService
from app.engine.station_worker import StationWorker
from app.repositories.ad_break_repo import AdBreakRepository
from app.repositories.queue_repo import QueueRepository


class CountingConnection(sqlite3.Connection):
    commits = 0
    fail_commit = False

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise sqlite3.OperationalError("simulated durability failure")
        return super().commit()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(path))
    init_db()
    conn = sqlite3.connect(path, factory=CountingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        "INSERT INTO tracks (id, station_id, title, artist, duration, track_type, file_path) "
        "VALUES (77, 1, 'Song', 'Artist', 180, 'music', 'C:/music/song.mp3')"
    )
    conn.commit()
    value = StationWorker.__new__(StationWorker)
    value.station_id = 1
    value.conn = conn
    value.queue_repo = QueueRepository(conn)
    value.ad_repo = AdBreakRepository(conn)
    value.playout_state = PlayoutStateService(conn)
    value.runtime_registry = None
    value._get_active_show_session = lambda: None
    value._record_ai_broadcast = lambda *_: None
    value._default_crossfade_seconds = lambda: 0.0
    monkeypatch.setattr("app.services.music_usage.request_music_usage_export", lambda: None)
    conn.commits = 0
    yield value
    conn.close()


def active_music(worker):
    item_id = worker.queue_repo.enqueue(1, 77)
    worker.queue_repo.mark_playing(item_id)
    worker.playout_state.set_current(1, "manual", item_id)
    worker.conn.commits = 0
    return worker.queue_repo.current_playing(1)


def test_completion_queue_track_usage_and_ownership_commit_together(worker):
    playing = active_music(worker)
    worker._complete_queue_item(playing)
    assert worker.conn.commits == 1
    assert worker.conn.execute("SELECT status FROM queue_items WHERE id=?", (playing["id"],)).fetchone()[0] == "done"
    assert worker.conn.execute("SELECT play_count FROM tracks WHERE id=77").fetchone()[0] == 1
    assert worker.conn.execute("SELECT log_id FROM music_usage_log").fetchone()[0] == f"queue:{playing['id']}"
    assert worker.playout_state.get_current(1) == {"source": "none", "item_id": None}
    assert isinstance(worker.conn, CountingConnection)
    assert worker.queue_repo.conn is worker.conn
    assert worker.playout_state.conn is worker.conn


def test_usage_export_waits_for_complete_durable_group(worker, monkeypatch):
    playing = active_music(worker)
    snapshots = []

    def export():
        with sqlite3.connect(worker.conn.execute("PRAGMA database_list").fetchone()[2]) as reader:
            snapshots.append((reader.execute("SELECT status FROM queue_items WHERE id=?", (playing["id"],)).fetchone()[0],
                              reader.execute("SELECT COUNT(*) FROM music_usage_log").fetchone()[0]))

    monkeypatch.setattr("app.services.music_usage.request_music_usage_export", export)
    worker._complete_queue_item(playing)
    assert snapshots == [("done", 1)]


def test_usage_failure_does_not_leave_half_completed_ownership(worker):
    playing = active_music(worker)
    worker.conn.execute("CREATE TRIGGER fail_usage BEFORE INSERT ON music_usage_log "
                        "BEGIN SELECT RAISE(ABORT, 'simulated failed usage writer'); END")
    worker.conn.commit()
    worker.conn.commits = 0
    with pytest.raises(CommitBatchAborted):
        worker._complete_queue_item(playing)
    assert worker.conn.commits == 0
    assert worker.queue_repo.current_playing(1)["id"] == playing["id"]
    assert worker.playout_state.get_current(1)["item_id"] == playing["id"]
    assert worker.conn.execute("SELECT play_count FROM tracks WHERE id=77").fetchone()[0] == 0
    assert worker.conn.execute("SELECT COUNT(*) FROM music_usage_log").fetchone()[0] == 0
    assert worker.queue_repo.conn is worker.conn


def test_next_item_is_durable_before_runtime_receives_audio(worker):
    item_id = worker.queue_repo.enqueue(1, 77)
    path = worker.conn.execute("PRAGMA database_list").fetchone()[2]
    starts = []

    def start(*args, **kwargs):
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT status FROM queue_items WHERE id=?", (item_id,)).fetchone()[0] == "playing"
            assert reader.execute("SELECT current_source, current_item_id FROM playout_state WHERE station_id=1").fetchone() == ("manual", item_id)
        starts.append(args)

    worker.runtime_registry = SimpleNamespace(start_station=start)
    worker.conn.commits = 0
    result = worker._play_managed_item(source="manual", item_id=item_id, track_id=77,
                                       mark_playing=worker.queue_repo.mark_playing,
                                       mark_done=worker.queue_repo.mark_done,
                                       mark_failed=worker.queue_repo.mark_failed,
                                       auto_done=False)
    assert result["source"] == "manual"
    assert len(starts) == 1
    assert worker.conn.commits == 1


def test_ad_completion_is_atomic_and_preserves_clean_eof_gate(worker):
    worker.conn.execute("UPDATE tracks SET track_type='ad' WHERE id=77")
    worker.conn.commit()
    item_id = worker.ad_repo.enqueue(1, 77, "2000-01-01 00:00:00")
    worker.ad_repo.mark_playing(item_id)
    worker.playout_state.set_current(1, "ads", item_id)
    worker._ads_enabled = lambda: True
    status = {"active_input_uri": "C:/music/song.mp3", "producer_eof": False,
              "program_running": False, "producer_draining": True}
    worker.runtime_registry = SimpleNamespace(status=lambda *_: dict(status))
    worker.conn.commits = 0
    assert worker._advance_playing_ad_item() is False
    assert worker.conn.commits == 0
    assert worker.ad_repo.current_playing(1)["id"] == item_id
    status.update(producer_eof=True, producer_draining=False)
    assert worker._advance_playing_ad_item() is True
    assert worker.conn.commits == 1
    assert worker.ad_repo.current_playing(1) is None
    assert worker.playout_state.get_current(1) == {"source": "none", "item_id": None}


def test_nested_boundary_group_retains_one_commit(worker):
    with worker._batch_boundary_writes():
        worker.conn.execute("UPDATE tracks SET title='Nested' WHERE id=77")
        with worker._batch_boundary_writes():
            worker.conn.execute("UPDATE tracks SET artist='Artist 2' WHERE id=77")
            worker.conn.commit()
    assert worker.conn.commits == 1
    assert isinstance(worker.conn, CountingConnection)


def test_ownership_commit_failure_never_starts_next_source(worker):
    item_id = worker.queue_repo.enqueue(1, 77)
    starts = []
    worker.runtime_registry = SimpleNamespace(start_station=lambda *a, **k: starts.append(a))
    worker.conn.fail_commit = True
    with pytest.raises(sqlite3.OperationalError, match="durability failure"):
        worker._play_managed_item(source="manual", item_id=item_id, track_id=77,
                                  mark_playing=worker.queue_repo.mark_playing,
                                  mark_done=worker.queue_repo.mark_done,
                                  mark_failed=worker.queue_repo.mark_failed,
                                  auto_done=False)
    assert starts == []
    assert worker.conn.execute("SELECT status FROM queue_items WHERE id=?", (item_id,)).fetchone()[0] == "pending"
    assert worker.playout_state.get_current(1)["source"] == "none"
    assert worker.queue_repo.conn is worker.conn
