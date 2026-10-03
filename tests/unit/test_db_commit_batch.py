import sqlite3

import pytest

from app.db_commit_batch import CommitBatchAborted, commit_batch


class CountingConnection(sqlite3.Connection):
    commits = 0
    fail_commit = False

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise sqlite3.OperationalError("simulated durability failure")
        return super().commit()


@pytest.fixture
def connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "batch.db", factory=CountingConnection)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("CREATE TABLE records (value TEXT)")
    conn.commit()
    conn.commits = 0
    yield conn
    conn.close()


def values(conn):
    return [row[0] for row in conn.execute("SELECT value FROM records")]


def test_repository_commits_form_one_durable_transaction(connection):
    with sqlite3.connect(connection.execute("PRAGMA database_list").fetchone()[2]) as reader:
        with commit_batch(connection) as batch:
            batch.execute("INSERT INTO records VALUES ('first')")
            batch.commit()
            batch.execute("INSERT INTO records VALUES ('second')")
            batch.commit()
            assert values(reader) == []
        assert values(reader) == ["first", "second"]
    assert connection.commits == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_autocommit_connection_is_still_atomic(connection):
    connection.isolation_level = None
    with pytest.raises(RuntimeError):
        with commit_batch(connection) as batch:
            batch.execute("INSERT INTO records VALUES ('rolled back')")
            batch.commit()
            raise RuntimeError("source bookkeeping failed")
    assert values(connection) == []
    assert connection.commits == 0


def test_caught_inner_rollback_poisons_entire_group(connection):
    notified = []
    with pytest.raises(CommitBatchAborted):
        with commit_batch(connection) as batch:
            batch.execute("INSERT INTO records VALUES ('partial')")
            batch.after_commit(lambda: notified.append(True))
            batch.rollback()
            with pytest.raises(CommitBatchAborted):
                batch.commit()
    assert values(connection) == []
    assert notified == []
    assert connection.commits == 0


def test_nested_connection_context_does_not_commit_early(connection):
    with commit_batch(connection) as batch:
        with batch:
            batch.execute("INSERT INTO records VALUES ('one')")
        assert connection.commits == 0
    assert connection.commits == 1
    assert values(connection) == ["one"]


def test_caught_nested_exception_aborts_group(connection):
    with pytest.raises(CommitBatchAborted):
        with commit_batch(connection) as batch:
            try:
                with batch:
                    batch.execute("INSERT INTO records VALUES ('partial')")
                    raise RuntimeError("best effort writer failed")
            except RuntimeError:
                pass
    assert values(connection) == []


def test_commit_failure_rolls_back_and_skips_notification(connection):
    notified = []
    connection.fail_commit = True
    with pytest.raises(sqlite3.OperationalError, match="durability failure"):
        with commit_batch(connection) as batch:
            batch.execute("INSERT INTO records VALUES ('partial')")
            batch.after_commit(lambda: notified.append(True))
    assert not connection.in_transaction
    assert values(connection) == []
    assert notified == []


def test_notification_runs_after_other_connections_can_read_commit(connection):
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    notified = []

    def notification():
        with sqlite3.connect(path) as reader:
            notified.extend(values(reader))

    with commit_batch(connection) as batch:
        batch.execute("INSERT INTO records VALUES ('durable')")
        batch.after_commit(notification)
        assert notified == []
    assert notified == ["durable"]


def test_notification_failure_cannot_undo_committed_records(connection):
    def fail():
        raise RuntimeError("UI disconnected")

    with commit_batch(connection) as batch:
        batch.execute("INSERT INTO records VALUES ('durable')")
        batch.after_commit(fail)
    assert values(connection) == ["durable"]


def test_existing_transaction_is_not_committed_or_rolled_back(connection):
    connection.execute("INSERT INTO records VALUES ('caller owned')")
    with pytest.raises(ValueError, match="idle connection"):
        with commit_batch(connection):
            pytest.fail("must not enter")
    assert connection.in_transaction
    assert connection.commits == 0
    assert values(connection) == ["caller owned"]


def test_abort_cannot_schedule_a_notification(connection):
    with pytest.raises(CommitBatchAborted):
        with commit_batch(connection) as batch:
            batch.rollback()
            batch.after_commit(lambda: None)
