from contextlib import contextmanager


@contextmanager
def transaction(conn):
    cur = conn.cursor()
    try:
        yield cur
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
