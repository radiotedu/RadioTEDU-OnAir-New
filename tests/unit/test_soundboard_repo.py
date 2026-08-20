from app.db import init_db, get_connection
from app.repositories.soundboard_repo import SoundboardRepository


def _repo():
    init_db()
    conn = get_connection()
    return SoundboardRepository(conn), conn


def test_create_and_get():
    repo, conn = _repo()
    item_id = repo.create(station_id=1, name="Jingle", file_path="/sfx/jingle.mp3")
    item = repo.get(item_id)
    assert item is not None
    assert item["name"] == "Jingle"
    assert item["file_path"] == "/sfx/jingle.mp3"
    assert item["color"] == "#4a90d9"
    assert item["gain_db"] == 0.0
    assert item["uploaded"] == 0
    conn.close()


def test_create_with_optional_fields():
    repo, conn = _repo()
    item_id = repo.create(
        station_id=1, name="Bip", file_path="/sfx/bip.wav",
        color="#ff0000", hotkey="1", category="effect",
        duration_s=1.5, gain_db=-3.0, sort_order=5, uploaded=1,
    )
    item = repo.get(item_id)
    assert item["color"] == "#ff0000"
    assert item["hotkey"] == "1"
    assert item["category"] == "effect"
    assert item["duration_s"] == 1.5
    assert item["gain_db"] == -3.0
    assert item["sort_order"] == 5
    assert item["uploaded"] == 1
    conn.close()


def test_list_by_station_sorted():
    repo, conn = _repo()
    conn.execute("INSERT INTO stations (id, name) VALUES (2, 'Station 2')")
    conn.commit()
    repo.create(station_id=1, name="B", file_path="/b.mp3", sort_order=2)
    repo.create(station_id=1, name="A", file_path="/a.mp3", sort_order=1)
    repo.create(station_id=2, name="C", file_path="/c.mp3", sort_order=0)
    items = repo.list_by_station(1)
    assert len(items) >= 2
    names = [i["name"] for i in items if i["station_id"] == 1]
    assert names.index("A") < names.index("B")
    conn.close()


def test_update():
    repo, conn = _repo()
    item_id = repo.create(station_id=1, name="Old", file_path="/old.mp3")
    repo.update(item_id, name="New", color="#00ff00")
    item = repo.get(item_id)
    assert item["name"] == "New"
    assert item["color"] == "#00ff00"
    conn.close()


def test_delete():
    repo, conn = _repo()
    item_id = repo.create(station_id=1, name="Del", file_path="/del.mp3")
    assert repo.delete(item_id) is True
    assert repo.get(item_id) is None
    assert repo.delete(item_id) is False
    conn.close()
