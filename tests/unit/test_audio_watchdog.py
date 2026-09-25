from __future__ import annotations

import pytest

from app.services.audio_watchdog import AudioWatchdogService, WATCHDOG_STATIONS


def test_snapshot_and_repair_allow_maincharacter_mount():
    assert WATCHDOG_STATIONS[11] == (
        "maincharacter",
        "http://stream.radiotedu.com:11154/maincharacter",
    )


def test_repair_restarts_only_selected_station(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "app.api.runtime.operator_stop_runtime",
        lambda station_id: calls.append(("stop", station_id)),
    )

    def _start(station_id, payload):
        calls.append(("start", station_id))
        return {"running": True, "worker_running": True}

    monkeypatch.setattr("app.api.runtime.operator_start_runtime_loop", _start)
    service = AudioWatchdogService()
    monkeypatch.setattr(service, "snapshot", lambda: {"managed_profiles_ok": True})

    result = service.repair(station_ids=[8, 8], repair_managed_profiles=False)

    assert result["ok"] is True
    assert calls == [("stop", 8), ("start", 8)]
    assert [item["station_id"] for item in result["restarted"]] == [8]


def test_repair_rejects_unknown_station_id():
    with pytest.raises(ValueError, match="invalid_watchdog_station_ids"):
        AudioWatchdogService().repair(station_ids=[999], repair_managed_profiles=False)


def test_repair_defers_healthy_station_without_force(monkeypatch):
    service = AudioWatchdogService()
    monkeypatch.setattr(
        service,
        "_runtime_snapshot",
        lambda station_id: {
            "running": True,
            "worker_running": True,
            "program_running": True,
            "output_running": True,
            "mount_healthy": True,
            "writer_failed": False,
            "network_failed": False,
            "last_write_age": 0.1,
        },
    )
    monkeypatch.setattr(service, "snapshot", lambda: {"managed_profiles_ok": True})

    result = service.repair(station_ids=[2], repair_managed_profiles=False)

    assert result["restarted"] == []
    assert result["deferred"][0]["station_id"] == 2


def test_repair_force_restarts_publicly_failed_healthy_station(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.api.runtime.operator_stop_runtime",
        lambda station_id: calls.append(("stop", station_id)),
    )
    monkeypatch.setattr(
        "app.api.runtime.operator_start_runtime_loop",
        lambda station_id, payload: calls.append(("start", station_id))
        or {"running": True, "worker_running": True},
    )
    service = AudioWatchdogService()
    monkeypatch.setattr(
        service,
        "_runtime_snapshot",
        lambda station_id: {
            "running": True,
            "worker_running": True,
            "program_running": True,
            "output_running": True,
            "mount_healthy": True,
            "writer_failed": False,
            "network_failed": False,
            "last_write_age": 0.1,
        },
    )
    monkeypatch.setattr(service, "snapshot", lambda: {"managed_profiles_ok": True})

    result = service.repair(
        station_ids=[2],
        force_station_ids=[2],
        repair_managed_profiles=False,
    )

    assert calls == [("stop", 2), ("start", 2)]
    assert result["forced_station_ids"] == [2]


def test_report_is_bounded_and_persisted(tmp_path, monkeypatch):
    service = AudioWatchdogService()
    monkeypatch.setattr(type(service), "state_root", property(lambda self: tmp_path))

    report = service.record_report(
        {
            "status": "ok",
            "message": "healthy",
            "failed_station_ids": [8, 999],
            "managed_profiles_ok": True,
        }
    )

    assert report["failed_station_ids"] == [8]
    assert report["managed_profiles_ok"] is True
    assert service._last_report()["status"] == "ok"


def test_watchdog_api_requires_auth_and_accepts_operator(client, admin_token_headers, monkeypatch):
    monkeypatch.setattr(
        "app.api.watchdog.audio_watchdog_service.snapshot",
        lambda: {"managed_profiles_ok": True, "stations": []},
    )

    unauthenticated = client.get(
        "/api/watchdog/status",
        headers={"X-Test-No-Auto-Auth": "1"},
    )
    assert unauthenticated.status_code == 401

    operator = client.get("/api/watchdog/status", headers=admin_token_headers)
    assert operator.status_code == 200
    assert operator.json()["managed_profiles_ok"] is True


def test_watchdog_report_accepts_only_watchdog_token(client, monkeypatch):
    monkeypatch.setattr("app.api.watchdog.watchdog_request_is_valid", lambda request: True)
    monkeypatch.setattr(
        "app.api.watchdog.audio_watchdog_service.record_report",
        lambda payload: {"status": payload["status"]},
    )

    response = client.post(
        "/api/watchdog/report",
        json={"status": "ok", "managed_profiles_ok": True},
        headers={"X-Test-No-Auto-Auth": "1"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
