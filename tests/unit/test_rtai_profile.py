from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import (
    _autostart_station_worker_loops,
    app,
)
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.repositories.station_repo import StationRepository


def _configure_output(repo: StationOutputRepository, station_id: int, mount: str) -> None:
    repo.upsert(
        station_id=station_id,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="stream.radiotedu.com",
        icecast_port=11154,
        icecast_mount=mount,
        icecast_user="source",
        icecast_password="test-source-password",
        stream_codec_profile="aac_plus_196",
        stream_bitrate_kbps=196,
    )


def test_saved_stop_prevents_only_that_station_from_autostarting(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    stations = StationRepository(conn)
    outputs = StationOutputRepository(conn)
    settings = SettingsRepository(conn)
    radio_id = int(stations.list_all()[0]["id"])
    rock_id = stations.create("RadioTEDU Rock")
    _configure_output(outputs, radio_id, "/radio")
    _configure_output(outputs, rock_id, "/rock")
    settings.upsert_station(radio_id, {"broadcast_autostart_enabled": "false"})
    settings.upsert_station(rock_id, {"broadcast_autostart_enabled": "true"})

    starts = []
    monkeypatch.setattr(
        "app.api.runtime.worker_loop_manager.start",
        lambda **kwargs: starts.append(dict(kwargs)),
    )

    _autostart_station_worker_loops(conn)

    assert [item["station_id"] for item in starts] == [rock_id]


def test_one_failed_station_never_blocks_sibling_boot_autostart(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    stations = StationRepository(conn)
    settings = SettingsRepository(conn)
    first_id = int(stations.list_all()[0]["id"])
    second_id = stations.create("RadioTEDU Rock")
    settings.upsert_station(first_id, {"broadcast_autostart_enabled": "true"})
    settings.upsert_station(second_id, {"broadcast_autostart_enabled": "true"})
    started = []

    def start(**kwargs):
        if int(kwargs["station_id"]) == first_id:
            raise RuntimeError("stale station lease")
        started.append(dict(kwargs))

    monkeypatch.setattr("app.api.runtime.worker_loop_manager.start", start)

    _autostart_station_worker_loops(conn)

    assert [item["station_id"] for item in started] == [second_id]


def test_onair_route_is_the_canonical_operator_shell(client: TestClient):
    response = client.get("/app")
    assert response.status_code == 200
    assert "RadioTEDU OnAir" in response.text
    assert "RADIOTEDU BROADCAST AUTOMATION" in response.text

    logo = client.get("/static/onair/assets/radiotedu-onair-logo.png")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/png"
    assert logo.content.startswith(b"\x89PNG\r\n\x1a\n")
