from fastapi.testclient import TestClient

from app.api import health_wall
from app.main import app


def _reset_cache(monkeypatch):
    monkeypatch.setattr(health_wall, "_fast_cache", None)
    monkeypatch.setattr(health_wall, "_slow_cache", None)


def test_passwordless_snapshot_is_loopback_only(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(
        health_wall,
        "_collect_fast",
        lambda: {"stations": [{"station_id": 1, "name": "RadioTEDU EN"}]},
    )
    monkeypatch.setattr(
        health_wall,
        "_collect_slow",
        lambda: {"library": {}, "integrations": {}, "services": {"items": []}},
    )

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/monitor/snapshot")
    assert response.status_code == 200
    assert response.json()["stations"][0]["name"] == "RadioTEDU EN"

    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        response = client.get("/api/monitor/snapshot")
    assert response.status_code == 403
    assert response.json()["detail"] == "health_wall_loopback_only"


def test_snapshot_uses_fast_and_slow_cache_tiers(monkeypatch):
    _reset_cache(monkeypatch)
    calls = {"fast": 0, "slow": 0}

    def fast():
        calls["fast"] += 1
        return {"stations": []}

    def slow():
        calls["slow"] += 1
        return {"library": {}, "integrations": {}, "services": {"items": []}}

    monkeypatch.setattr(health_wall, "_collect_fast", fast)
    monkeypatch.setattr(health_wall, "_collect_slow", slow)
    monkeypatch.setattr(health_wall, "_FAST_CACHE_TTL_SECONDS", 60.0)
    monkeypatch.setattr(health_wall, "_SLOW_CACHE_TTL_SECONDS", 60.0)

    with TestClient(app, client=("::1", 50000)) as client:
        first = client.get("/api/monitor/snapshot")
        second = client.get("/api/monitor/snapshot")

    assert first.status_code == second.status_code == 200
    assert calls == {"fast": 1, "slow": 1}


def test_slow_snapshot_includes_sanitized_public_stream_evidence(monkeypatch):
    class _Connection:
        def close(self):
            return None

    class _Settings:
        def __init__(self, _connection):
            pass

        def get_system(self):
            return {"stream_public_base_url": "https://public.example"}

    class _Evidence:
        def snapshot(self, settings):
            assert settings == {"stream_public_base_url": "https://public.example"}
            return {
                "state": "healthy",
                "configured": True,
                "observed_at": 123,
                "streams": {
                    "ai": {"state": "healthy", "dns": "reachable"},
                    "event": {"state": "unavailable", "dns": "reachable"},
                },
            }

    monkeypatch.setattr(health_wall, "init_db", lambda: None)
    monkeypatch.setattr(health_wall, "get_connection", _Connection)
    monkeypatch.setattr(health_wall, "SettingsRepository", _Settings)
    monkeypatch.setattr(health_wall, "_service_snapshot", lambda _raw: {"items": []})
    monkeypatch.setattr(health_wall, "get_public_stream_evidence_service", lambda: _Evidence())
    monkeypatch.setattr(health_wall, "get_managed_library_watcher", lambda: type("W", (), {"snapshot": lambda self: {}})())
    monkeypatch.setattr(health_wall, "get_unified_media_folder_service", lambda: type("U", (), {"status": lambda self: {}})())
    monkeypatch.setattr(health_wall, "get_product_media_catalog_service", lambda: type("C", (), {"snapshot": lambda self: {}})())

    payload = health_wall._collect_slow()

    assert payload["integrations"]["public_ai"] == "healthy"
    assert payload["integrations"]["public_event"] == "unavailable"
    assert payload["public_stream_evidence"]["streams"]["ai"]["dns"] == "reachable"
    assert "public.example" not in repr(payload)


def test_sanitizers_remove_paths_urls_and_raw_errors():
    watcher = health_wall._safe_watcher(
        {
            "running": True,
            "profiles": [
                {
                    "station_id": 1,
                    "track_type": "music",
                    "folder": "C:/secret/music",
                    "error": "C:/secret/file.mp3 failed",
                    "status": "ready",
                }
            ],
        }
    )
    product = health_wall._safe_product_catalog(
        {
            "products": [
                {
                    "product": "juke",
                    "directory": "Juke/Non-Turkish",
                    "database": "Databases/juke.sqlite3",
                    "state": "ready",
                    "file_count": 10,
                }
            ]
        }
    )
    services = health_wall._safe_services(
        [
            {
                "id": "juke_media_agent",
                "name": "Juke",
                "enabled": True,
                "state": "healthy",
                "source": {"path": "C:/secret/server.js"},
                "health": [{"url": "http://127.0.0.1:3210/v1/status", "ok": True}],
            }
        ]
    )

    flattened = repr({"watcher": watcher, "product": product, "services": services})
    assert "C:/secret" not in flattened
    assert "127.0.0.1:3210" not in flattened
    assert "directory" not in product["products"][0]
    assert services[0]["health_checks_ok"] == 1


def test_microphone_snapshot_reports_live_levels_without_raw_errors(monkeypatch):
    monkeypatch.setattr(
        health_wall.live_mic_registry,
        "snapshot",
        lambda _station_id: {
            "transmitting": True,
            "receiving": True,
            "live_input_enabled": True,
            "transport": "websocket",
            "source_name": "Studio microphone",
            "level_db": -12.5,
            "peak_db": -0.2,
            "buffer_bytes": 4096,
            "last_error": "C:/private/capture-device failed once",
        },
    )

    payload = health_wall._safe_microphone(1)

    assert payload["state"] == "healthy"
    assert payload["receiving"] is True
    assert payload["clipping"] is True
    assert payload["has_error"] is True
    assert "last_error" not in payload
    assert "private" not in repr(payload)


def test_microphone_snapshot_includes_passive_physical_device_readiness(monkeypatch):
    monkeypatch.setattr(
        health_wall.live_mic_registry,
        "snapshot",
        lambda _station_id: {"transmitting": True, "receiving": True},
    )
    monkeypatch.setattr(
        health_wall.physical_microphone_readiness,
        "snapshot",
        lambda **kwargs: {
            "presence": "present",
            "selection": "selected",
            "live": "live" if kwargs["live"] else "idle",
            "receiving": "receiving" if kwargs["receiving"] else "not-receiving",
            "label": "",
            "refreshing": False,
        },
    )

    payload = health_wall._safe_microphone(1)

    assert payload["physical_device"] == {
        "presence": "present",
        "selection": "selected",
        "live": "live",
        "receiving": "receiving",
        "label": "",
        "refreshing": False,
    }


def test_microphone_configured_device_missing_degrades_without_leaking_source_label(monkeypatch):
    monkeypatch.setattr(
        health_wall.live_mic_registry,
        "snapshot",
        lambda _station_id: {"transmitting": True, "receiving": True, "source_name": "C:/Users/private/USB Mic"},
    )
    monkeypatch.setattr(
        health_wall.physical_microphone_readiness,
        "snapshot",
        lambda **_kwargs: {"presence": "present", "selection": "not-present", "label": "Private Mic", "stale": False},
    )
    payload = health_wall._safe_microphone(1)
    assert payload["state"] == "degraded"
    assert payload["source_name"] == ""
    assert "Private Mic" not in repr(payload)


def test_microphone_snapshot_normalizes_malformed_and_non_finite_metrics(monkeypatch):
    monkeypatch.setattr(
        health_wall.live_mic_registry,
        "snapshot",
        lambda _station_id: {
            "transmitting": True,
            "receiving": True,
            "level_db": float("nan"),
            "peak_db": float("inf"),
            "buffer_bytes": "not-a-number",
        },
    )

    payload = health_wall._safe_microphone(1)

    assert payload["level_db"] == -60.0
    assert payload["peak_db"] == -60.0
    assert payload["buffer_bytes"] == 0


def test_runtime_health_requires_output_connection_when_explicit():
    assert health_wall._runtime_health({"running": True, "liquidsoap_connected": True}) == "healthy"
    assert health_wall._runtime_health({"running": True, "liquidsoap_connected": False}) == "degraded"
    assert health_wall._runtime_health({"running": True, "branch_health": {"icecast": False}}) == "degraded"
    assert health_wall._runtime_health(
        {
            "running": True,
            "branch_health": {"icecast": True},
            "delivery_health": {"icecast": False},
            "required_outputs": {"icecast": True},
        }
    ) == "degraded"
    assert health_wall._runtime_health(
        {
            "running": True,
            "delivery_health": {"icecast": True, "local": False},
            "required_outputs": {"icecast": True, "local": False},
        }
    ) == "healthy"
    assert health_wall._runtime_health({"running": False}) == "unavailable"
