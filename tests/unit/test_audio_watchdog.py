from __future__ import annotations

from pathlib import Path

import pytest

from app.services.audio_watchdog import AudioWatchdogService, _same_windows_path


WATCHDOG_SCRIPT = (
    Path(__file__).resolve().parents[2] / "tools" / "RadioTEDU-AudioWatchdog.ps1"
)


def test_windows_path_comparison_normalizes_duplicate_separators():
    assert _same_windows_path(
        r"H:\\RadioTEDU Songs\\Rock", r"H:\RadioTEDU Songs\Rock"
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
    monkeypatch.setattr(service, "_runtime_snapshot", lambda _station_id: {"running": False})

    result = service.repair(station_ids=[8, 8], repair_managed_profiles=False)

    assert result["ok"] is True
    assert calls == [("stop", 8), ("start", 8)]
    assert [item["station_id"] for item in result["restarted"]] == [8]


def test_repair_preserves_worker_when_public_probe_disagrees_with_healthy_source(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "app.api.runtime.operator_stop_runtime",
        lambda station_id: calls.append(("stop", station_id)),
    )
    service = AudioWatchdogService()
    monkeypatch.setattr(service, "snapshot", lambda: {"managed_profiles_ok": True})
    monkeypatch.setattr(
        service,
        "_runtime_snapshot",
        lambda _station_id: {
            "running": True,
            "worker_running": True,
            "program_running": True,
            "output_running": True,
            "mount_healthy": True,
        },
    )

    result = service.repair(station_ids=[8], repair_managed_profiles=False)

    assert calls == []
    assert result["restarted"] == []
    assert result["deferred"] == [
        {
            "station_id": 8,
            "reason": "public_probe_disagreed_with_healthy_source",
        }
    ]


def test_repair_rejects_unknown_station_id():
    with pytest.raises(ValueError, match="invalid_watchdog_station_ids"):
        AudioWatchdogService().repair(station_ids=[999], repair_managed_profiles=False)


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


def test_public_only_failure_preserves_healthy_source_worker():
    script = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    assert "$repairableFailed = @($locallyUnhealthyFailed)" in script
    assert "healthy workers were preserved" in script
    assert "$deferredCount -gt 0" in script
    assert "recovered before repair; worker restart suppressed" in script
    assert "forcing source re-registration" not in script


def test_repair_cooldown_is_saved_only_after_final_verification():
    script = WATCHDOG_SCRIPT.read_text(encoding="utf-8")

    save_position = script.rindex("Save-RepairState")
    final_failure_position = script.index(
        'Send-Report "failed" "Repair completed but final verification still failed."'
    )
    assert save_position > final_failure_position
