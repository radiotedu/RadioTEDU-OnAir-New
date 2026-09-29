from datetime import datetime, timedelta, timezone

from app.db import get_connection, init_db
from app.repositories.ad_break_repo import AdBreakRepository


def test_defer_preserves_campaign_due_time_and_keeps_ad_retryable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    repo = AdBreakRepository(conn)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    first_due = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    second_due = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    deferred_id = repo.enqueue(1, 77, first_due, dedupe_key="retry-ad")
    ready_id = repo.enqueue(1, 78, second_due, dedupe_key="ready-ad")
    repo.mark_playing(deferred_id)

    assert repo.defer(deferred_id, 3600, error="temporary sink failure") == 1

    deferred = conn.execute(
        "SELECT status, due_at, started_at, finished_at, retry_after, retry_count, last_error "
        "FROM ad_break_items WHERE id=?",
        (deferred_id,),
    ).fetchone()
    assert deferred["status"] == "pending"
    assert deferred["due_at"] == first_due
    assert deferred["started_at"] is None
    assert deferred["finished_at"] is None
    assert deferred["retry_after"] is not None
    assert deferred["retry_count"] == 1
    assert deferred["last_error"] == "temporary sink failure"
    assert repo.current_playing(1) is None
    assert repo.next_due(1)["id"] == ready_id

    # The active dedupe key survives deferral, so plan reconciliation cannot
    # create a duplicate occurrence while this row waits for retry.
    assert repo.enqueue(1, 77, first_due, dedupe_key="retry-ad") == deferred_id

    conn.execute(
        "UPDATE ad_break_items SET retry_after=datetime(CURRENT_TIMESTAMP, '-1 seconds') "
        "WHERE id=?",
        (deferred_id,),
    )
    conn.commit()
    assert repo.next_due(1)["id"] == deferred_id
