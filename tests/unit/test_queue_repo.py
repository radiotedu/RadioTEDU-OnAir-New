from app.db import get_connection, init_db
from app.repositories.queue_repo import QueueRepository


def test_enqueue_and_next_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = QueueRepository(get_connection())
    item_id = repo.enqueue(station_id=1, track_id=77, dedupe_key="a1")
    assert item_id > 0
    nxt = repo.next_pending(station_id=1)
    assert nxt["track_id"] == 77
    repo.mark_playing(item_id)
    repo.mark_done(item_id)


def test_enqueue_or_get_existing_dedupes_pending_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = QueueRepository(get_connection())

    id1, created1 = repo.enqueue_or_get_existing(
        station_id=1, track_id=77, dedupe_key="q:1:77"
    )
    id2, created2 = repo.enqueue_or_get_existing(
        station_id=1, track_id=77, dedupe_key="q:1:77"
    )

    assert created1 is True
    assert created2 is False
    assert id1 == id2
