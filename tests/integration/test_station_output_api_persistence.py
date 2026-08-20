from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


def test_station_output_persists_and_enforces_max_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    for station_id in range(1, 5):
        res = client.post(
            "/api/stations/output",
            json={
                "station_id": station_id,
                "local_output_enabled": True,
                "output_device_id": f"dev{station_id}",
                "icecast_mount": f"/station{station_id}",
            },
        )
        assert res.status_code == 200

    fifth = client.post(
        "/api/stations/output",
        json={
            "station_id": 5,
            "local_output_enabled": True,
            "output_device_id": "dev5",
            "icecast_mount": "/station5",
        },
    )
    assert fifth.status_code == 409

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM station_outputs")
    assert cur.fetchone()[0] == 4


def test_station_output_persistence_allows_system_default_device_but_rejects_blank_mount(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    client = TestClient(app)

    missing_device = client.post(
        "/api/stations/output",
        json={
            "station_id": 21,
            "local_output_enabled": True,
            "output_device_id": "",
            "icecast_enabled": False,
        },
    )
    assert missing_device.status_code == 200
    assert missing_device.json()["output"]["output_device_id"] == ""

    missing_mount = client.post(
        "/api/stations/output",
        json={
            "station_id": 22,
            "local_output_enabled": False,
            "icecast_enabled": True,
            "icecast_mount": "",
        },
    )
    assert missing_mount.status_code == 400
