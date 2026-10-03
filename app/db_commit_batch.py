"""Group related playout mutations without weakening SQLite durability."""

import logging
from contextlib import contextmanager

_log = logging.getLogger("cleanroom.commit_batch")


class CommitBatchAborted(RuntimeError):
    pass


class CommitBatchConnection:
    """Delegate SQL to one connection; defer commits until the enclosing scope.

    Repositories retain their usual commit calls. A rollback or a failing nested
    connection context poisons the entire group, including errors caught by a
    best-effort reporting caller. Post-commit notifications are never emitted
    for an aborted group.
    """

    def __init__(self, connection):
        self.connection = connection
        self.aborted = False
        self._after_commit = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        if self.aborted:
            raise CommitBatchAborted("playout write group was rolled back")

    def rollback(self):
        self.aborted = True
        self._after_commit.clear()
        self.connection.rollback()

    def after_commit(self, callback):
        if self.aborted:
            raise CommitBatchAborted("playout write group was rolled back")
        self._after_commit.append(callback)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.rollback()
        return False


@contextmanager
def commit_batch(connection):
    if connection.in_transaction:
        raise ValueError("commit batch requires an idle connection")
    batch = CommitBatchConnection(connection)
    try:
        # An explicit transaction also preserves atomicity for connections
        # opened in autocommit mode. Never commit a caller's existing work.
        connection.execute("BEGIN")
        yield batch
        if batch.aborted:
            raise CommitBatchAborted("playout write group was rolled back")
        # Retain the connection's configured journal/synchronous policy. This
        # commit is the same durability barrier as the repositories used alone.
        connection.commit()
    except BaseException:
        batch._after_commit.clear()
        connection.rollback()
        raise
    for callback in batch._after_commit:
        try:
            callback()
        except Exception:
            _log.debug("Post-commit notification failed", exc_info=True)
