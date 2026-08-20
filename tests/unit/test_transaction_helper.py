from app.db import get_connection, init_db
from app.repositories.common import transaction


def test_transaction_rolls_back_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    try:
        with transaction(conn) as cur:
            cur.execute("INSERT INTO stations (id, name) VALUES (?, ?)", (2, "A"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM stations WHERE id=2")
    assert cur.fetchone()[0] == 0
