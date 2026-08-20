from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.repositories.station_output_repo import StationOutputRepository


def test_station_output_runtime_supports_local_plus_icecast_for_multiple_stations(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    station_a = client.post(
        "/api/stations/output",
        json={
            "station_id": 1,
            "local_output_enabled": True,
            "output_device_id": "dev-a",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/a",
            "icecast_user": "source",
            "icecast_password": "pw",
        },
    )
    station_b = client.post(
        "/api/stations/output",
        json={
            "station_id": 2,
            "local_output_enabled": True,
            "output_device_id": "dev-b",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/b",
            "icecast_user": "source",
            "icecast_password": "pw",
        },
    )

    assert station_a.status_code == 200
    assert station_b.status_code == 200

    repo = StationOutputRepository(get_connection())
    assert repo.count_active_local_outputs() == 2

    duplicate_device = client.post(
        "/api/stations/output",
        json={
            "station_id": 3,
            "local_output_enabled": True,
            "output_device_id": "dev-a",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/c",
            "icecast_user": "source",
            "icecast_password": "pw",
        },
    )
    assert duplicate_device.status_code == 409
