from app.db import get_connection, init_db
from app.repositories.outbox_repo import OutboxRepository


def test_enqueue_and_claim_pending_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = OutboxRepository(get_connection())
    cmd_id = repo.enqueue(1, "queue_push", {"track_id": 77})
    assert cmd_id > 0
    cmd = repo.claim_next(1)
    assert cmd is not None
    assert cmd["id"] == cmd_id
    repo.mark_done(cmd_id)
