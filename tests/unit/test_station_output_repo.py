from app.db import get_connection, init_db
from app.repositories.station_output_repo import StationOutputRepository


def test_upsert_and_count_active_local_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = StationOutputRepository(get_connection())

    repo.upsert(
        station_id=1,
        local_output_enabled=True,
        output_device_id="dev1",
        icecast_enabled=True,
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        stream_codec_profile="mp3_128",
        stream_bitrate_kbps=128,
        source_protocol="shoutcast",
    )

    cfg = repo.get(1)
    assert cfg is not None
    assert cfg["local_output_enabled"] == 1
    assert cfg["output_device_id"] == "dev1"
    assert cfg["icecast_enabled"] == 1
    assert cfg["icecast_host"] == "127.0.0.1"
    assert cfg["icecast_mount"] == "/station1"
    assert cfg["stream_codec_profile"] == "mp3_128"
    assert cfg["stream_bitrate_kbps"] == 128
    assert cfg["source_protocol"] == "shoutcast"
    assert repo.count_active_local_outputs() == 1


def test_output_repository_rejects_unknown_source_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = StationOutputRepository(get_connection())

    try:
        repo.upsert(
            station_id=1,
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="test-password",
            source_protocol="unknown",
        )
    except ValueError as exc:
        assert "source_protocol" in str(exc)
    else:
        raise AssertionError("unknown protocol was accepted")


def test_output_repository_finds_duplicate_enabled_stream_destination(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    repo = StationOutputRepository(get_connection())
    repo.upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="Stream.Example.org",
        icecast_port=8000,
        icecast_mount="/radio",
        icecast_user="source",
        icecast_password="secret",
    )

    monkeypatch.setattr(
        "app.repositories.station_output_repo.socket.getaddrinfo",
        lambda host, *_args, **_kwargs: [
            (2, 1, 6, "", ("10.0.0.7", 8000))
        ] if str(host).casefold() in {"stream.example.org", "10.0.0.7"} else [],
    )
    conflict = repo.find_active_stream_conflict(
        station_id=7,
        host="10.0.0.7",
        port=8000,
        mount="/radio",
        source_protocol="icecast",
    )

    assert conflict is not None
    assert int(conflict["station_id"]) == 1
    assert repo.find_active_stream_conflict(
        station_id=7,
        host="stream.example.org",
        port=8000,
        mount="/main",
        source_protocol="icecast",
    ) is None
