from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_repo import StationRepository


def test_station_and_settings_repositories_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    stations = StationRepository(conn)
    settings = SettingsRepository(conn)

    station_id = stations.create("Main")
    stations.set_active(station_id)
    assert stations.get_active()["id"] == station_id

    settings.upsert_system({"auto_scan": "1"})
    settings.upsert_station(station_id, {"duck_db": "-12"})
    assert settings.get_system()["auto_scan"] == "1"
    assert settings.get_station(station_id)["duck_db"] == "-12"
