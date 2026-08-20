from app.db import get_connection
from app.api.setup import (
    CODEC_PRESETS,
    _icecast_test_protocol_args,
    _normalize_stream_profile,
    _output_payload,
)
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository


def _make_dependencies_ready(monkeypatch):
    monkeypatch.setattr(
        "app.api.setup.describe_dependency",
        lambda *names: {
            "found": True,
            "path": f"C:/tools/{names[0]}",
            "source": "test",
            "managed_path": f"C:/tools/{names[0]}",
            "bootstrap_status": "ready",
            "bootstrap_error": "",
        },
    )
    monkeypatch.setattr(
        "app.api.setup._dependency_state",
        lambda: {
            "webview2": {"installed": True, "status": "ready"},
            "ollama": {"installed": True, "status": "ready"},
            "python_runtime": {"installed": True, "status": "ready"},
            "qwen_tts_runtime": {"installed": True, "status": "ready"},
        },
    )


def _make_ai_ready(monkeypatch):
    monkeypatch.setattr(
        "app.api.setup._ai_status",
        lambda settings: {
            "enabled": True,
            "ready": True,
            "llm_loaded": True,
            "tts_loaded": True,
            "tts_provider": settings.get("ai_tts_provider", "edge-tts"),
            "ollama_running": True,
        },
    )
    monkeypatch.setattr(
        "app.api.setup._startup_ai_readiness",
        lambda station_id, station_settings: {
            "state": "ready",
            "required_ready_track_intros": 1,
            "ready_track_intros": 1,
            "ready": True,
            "message": "AI startup buffer is primed for the next intro.",
            "prefetch": {},
        },
    )


def _make_stream_reachable(monkeypatch, reachable=True):
    monkeypatch.setattr("app.api.setup._tcp_reachable", lambda host, port: reachable)
    monkeypatch.setattr(
        "app.api.setup._run_stream_output_test",
        lambda output: {
            "ok": bool(reachable),
            "message": "Icecast accepted the setup test stream." if reachable else "Icecast test stream failed.",
        },
    )


def test_icecast_test_protocol_args_match_tls_production_transport():
    assert _icecast_test_protocol_args(
        {"icecast_port": 443, "icecast_tls_enabled": True}
    ) == ["-tls", "1"]
    assert _icecast_test_protocol_args(
        {"icecast_port": 8000, "icecast_tls_enabled": False}
    ) == []
    assert _icecast_test_protocol_args(
        {"icecast_port": 8000, "icecast_legacy_source_enabled": True}
    ) == ["-legacy_icecast", "1"]


def test_setup_output_payload_preserves_station_tls_transport_setting(client):
    conn = get_connection()
    StationOutputRepository(conn).upsert(
        station_id=1,
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        icecast_host="stream.example.test",
        icecast_port=443,
        icecast_mount="/secure",
        icecast_user="source",
        icecast_password="secret",
        output_gain_db=0,
    )
    settings = {"icecast_tls_enabled": "true"}
    assert _output_payload(conn, 1, settings)["icecast_tls_enabled"] is True
    conn.close()


def _configure_setup_via_api(client, *, ai_enabled=True, profile="opus_192"):
    res = client.post(
        "/api/setup/configure",
        json={
            "station_id": 1,
            "station_name": "RadioTEDU Broadcast Wall",
            "local_output_enabled": True,
            "output_device_id": "default-speakers",
            "icecast_enabled": True,
            "icecast_url": "http://127.0.0.1:8000/live",
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/live",
            "icecast_user": "source",
            "icecast_password": "hackme",
            "stream_codec_profile": profile,
            "ai_enabled": ai_enabled,
            "ai_warmth": "warm",
        },
    )
    assert res.status_code == 200, res.text
    station_id = int(res.json()["station_id"])
    if ai_enabled:
        conn = get_connection()
        try:
            SettingsRepository(conn).upsert_station(
                station_id,
                {
                    "ai_host_enabled": "true",
                    "ai_tts_provider": "local-qwen-tts",
                    "ai_voice_persona": "warm_radio_host",
                    "setup.ai_tts_test_passed": "true",
                    "setup.ai_tts_test_message": "AI voice test succeeded.",
                },
            )
        finally:
            conn.close()
    return station_id


def test_setup_state_blocks_completion_until_outputs_and_ai_are_verified(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    station_id = _configure_setup_via_api(client)

    state = client.get("/api/setup/state", params={"station_id": station_id}).json()
    assert state["completed"] is False
    assert state["can_complete"] is False
    assert {"local_output", "stream_output", "ai_tts"}.issubset(set(state["required_checks"]))

    for check in ("local_output", "stream_output", "ai_tts"):
        res = client.post("/api/setup/verify", json={"station_id": station_id, "check": check})
        assert res.status_code == 200, res.text

    complete = client.post("/api/setup/complete", json={"station_id": station_id})
    assert complete.status_code == 200, complete.text
    payload = complete.json()
    assert payload["completed"] is True
    assert payload["can_complete"] is True


def test_setup_completion_is_invalidated_when_stream_configuration_changes(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    station_id = _configure_setup_via_api(client)

    for check in ("local_output", "stream_output", "ai_tts"):
        assert client.post("/api/setup/verify", json={"station_id": station_id, "check": check}).status_code == 200
    assert client.post("/api/setup/complete", json={"station_id": station_id}).json()["completed"] is True

    conn = get_connection()
    try:
        StationOutputRepository(conn).upsert(
            station_id=station_id,
            local_output_enabled=True,
            output_device_id="default-speakers",
            icecast_enabled=True,
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/changed",
            icecast_user="source",
            icecast_password="hackme",
            stream_codec_profile="opus_192",
            stream_bitrate_kbps=192,
        )
    finally:
        conn.close()

    state = client.get("/api/setup/state", params={"station_id": station_id}).json()
    assert state["completed"] is False
    stream_check = next(check for check in state["checks"] if check["name"] == "stream_output")
    assert stream_check["ready"] is False


def test_setup_stream_verification_retries_after_destination_becomes_reachable(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    reachable = {"value": False}
    monkeypatch.setattr("app.api.setup._tcp_reachable", lambda host, port: reachable["value"])
    monkeypatch.setattr(
        "app.api.setup._run_stream_output_test",
        lambda output: {
            "ok": reachable["value"],
            "message": "Icecast accepted the setup test stream." if reachable["value"] else "Icecast test stream failed.",
        },
    )

    station_id = _configure_setup_via_api(client, ai_enabled=False)

    blocked = client.post("/api/setup/verify", json={"station_id": station_id, "check": "stream_output"})
    assert blocked.status_code == 409
    assert "not reachable" in blocked.text

    reachable["value"] = True
    verified = client.post("/api/setup/verify", json={"station_id": station_id, "check": "stream_output"})
    assert verified.status_code == 200, verified.text
    stream_check = next(check for check in verified.json()["checks"] if check["name"] == "stream_output")
    assert stream_check["ready"] is True


def test_setup_state_marks_disabled_ai_checks_optional(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)
    station_id = _configure_setup_via_api(client, ai_enabled=False)

    state = client.get("/api/setup/state", params={"station_id": station_id}).json()
    checks = {check["name"]: check for check in state["checks"]}

    assert checks["local_tts_runtime"]["required"] is False
    assert checks["local_tts_runtime"]["status"] == "ready"
    assert checks["ai_tts"]["required"] is False
    assert checks["ai_tts"]["status"] == "ready"
    assert "local_tts_runtime" not in state["required_checks"]
    assert "ai_tts" not in state["required_checks"]


def test_setup_stream_verification_runs_real_icecast_publish_test(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    monkeypatch.setattr("app.api.setup._tcp_reachable", lambda host, port: True)
    calls = []

    def _fake_stream_test(output):
        calls.append(dict(output))
        return {"ok": True, "message": "Icecast accepted the source login and mount."}

    monkeypatch.setattr("app.api.setup._run_stream_output_test", _fake_stream_test)

    station_id = _configure_setup_via_api(client, ai_enabled=False, profile="opus_192")

    res = client.post("/api/setup/verify", json={"station_id": station_id, "check": "stream_output"})

    assert res.status_code == 200, res.text
    assert calls
    assert calls[0]["icecast_host"] == "127.0.0.1"
    assert calls[0]["icecast_mount"] == "/live"
    assert calls[0]["icecast_user"] == "source"
    assert calls[0]["icecast_password"] == "hackme"
    assert calls[0]["stream_codec_profile"] == "opus_192"
    assert calls[0]["stream_bitrate_kbps"] == 192
    stream_check = next(check for check in res.json()["checks"] if check["name"] == "stream_output")
    assert stream_check["ready"] is True
    assert stream_check["details"]["test_passed"] is True


def test_setup_stream_verification_uses_healthy_live_encoder_without_second_source(
    client, monkeypatch
):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    monkeypatch.setattr("app.api.setup._tcp_reachable", lambda host, port: True)
    monkeypatch.setattr(
        "app.api.setup._live_stream_output_verification",
        lambda station_id: {
            "ok": True,
            "message": "Verified by the active encoder without interrupting the live mount.",
        },
    )

    def _unexpected_second_source(output):
        raise AssertionError("an occupied live mount must not receive a second source probe")

    monkeypatch.setattr(
        "app.api.setup._run_stream_output_test",
        _unexpected_second_source,
    )
    station_id = _configure_setup_via_api(client, ai_enabled=False)

    res = client.post(
        "/api/setup/verify",
        json={"station_id": station_id, "check": "stream_output"},
    )

    assert res.status_code == 200, res.text
    stream_check = next(
        check for check in res.json()["checks"] if check["name"] == "stream_output"
    )
    assert stream_check["details"]["test_passed"] is True
    assert "without interrupting" in stream_check["details"]["test_message"]


def test_setup_stream_verification_blocks_when_source_credentials_fail(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    monkeypatch.setattr("app.api.setup._tcp_reachable", lambda host, port: True)
    monkeypatch.setattr(
        "app.api.setup._run_stream_output_test",
        lambda output: {"ok": False, "message": "Icecast test stream failed: authentication failed"},
    )

    station_id = _configure_setup_via_api(client, ai_enabled=False)

    res = client.post("/api/setup/verify", json={"station_id": station_id, "check": "stream_output"})

    assert res.status_code == 409
    assert "authentication failed" in res.text
    state = client.get("/api/setup/state", params={"station_id": station_id}).json()
    stream_check = next(check for check in state["checks"] if check["name"] == "stream_output")
    assert stream_check["ready"] is False
    assert stream_check["details"]["test_passed"] is False


def test_setup_configure_parses_operator_icecast_url(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    res = client.post(
        "/api/setup/configure",
        json={
            "station_id": 1,
            "station_name": "Operator Station",
            "local_output_enabled": True,
            "output_device_id": "",
            "icecast_enabled": True,
            "icecast_url": "https://source:secretpass@stream.example.test:8443/radiotedu",
            "icecast_host": "",
            "icecast_port": 8000,
            "icecast_mount": "",
            "icecast_user": "source",
            "icecast_password": "",
            "stream_codec_profile": "opus_192",
            "ai_enabled": False,
            "ai_warmth": "warm",
        },
    )

    assert res.status_code == 200, res.text
    config = res.json()["config"]
    assert config["icecast_url"] == "http://stream.example.test:8443/radiotedu"
    assert config["icecast_host"] == "stream.example.test"
    assert config["icecast_port"] == 8443
    assert config["icecast_mount"] == "/radiotedu"
    assert config["icecast_user"] == "source"
    assert config["icecast_password"] == ""
    assert config["icecast_password_configured"] is True

    conn = get_connection()
    try:
        output_repo = StationOutputRepository(conn)
        raw_output = output_repo.get_raw(1)
        assert str(raw_output["icecast_password"]).startswith(
            "credential://user/station/1/"
        )
        assert raw_output["icecast_password"] != "secretpass"
        assert output_repo.get(1)["icecast_password"] == "secretpass"
        assert SettingsRepository(conn).get_station(1)["icecast_password"] == ""
    finally:
        conn.close()


def test_setup_allows_system_default_monitor_without_device_enumeration(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    conn = get_connection()
    try:
        SettingsRepository(conn).upsert_station(
            1,
            {
                "ai_host_enabled": "false",
            },
        )
        StationOutputRepository(conn).upsert(
            station_id=1,
            local_output_enabled=True,
            output_device_id="",
            icecast_enabled=True,
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/live",
            icecast_user="source",
            icecast_password="hackme",
            stream_codec_profile="opus_192",
            stream_bitrate_kbps=192,
        )
    finally:
        conn.close()

    res = client.post("/api/setup/verify", json={"station_id": 1, "check": "local_output"})

    assert res.status_code == 200, res.text
    local_check = next(check for check in res.json()["checks"] if check["name"] == "local_output")
    assert local_check["ready"] is True


def test_setup_configure_resolves_invalid_station_to_active_station(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    res = client.post(
        "/api/setup/configure",
        json={
            "station_id": 9999,
            "station_name": "Upgrade Safe Station",
            "local_output_enabled": True,
            "output_device_id": "",
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "live",
            "icecast_user": "source",
            "icecast_password": "hackme",
            "stream_codec_profile": "mp3_128",
            "ai_enabled": False,
            "ai_warmth": "warm",
        },
    )

    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["station_id"] == 1
    assert payload["station"]["name"] == "Upgrade Safe Station"
    assert payload["config"]["icecast_mount"] == "/live"
    assert payload["config"]["stream_codec_profile"] == "opus_192"


def test_setup_exposes_only_the_approved_opus_quality_presets():
    assert [
        (item["id"], item["bitrate_kbps"])
        for item in CODEC_PRESETS
    ] == [("opus_192", 192)]
    assert _normalize_stream_profile("opus_128", 128) == ("opus_192", 192)
    assert _normalize_stream_profile("mp3_128", 128) == ("opus_192", 192)


def test_setup_complete_issues_deployment_certificate(client, monkeypatch):
    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    _make_stream_reachable(monkeypatch)

    station_id = _configure_setup_via_api(client, ai_enabled=False)

    for check in ("local_output", "stream_output"):
        assert client.post("/api/setup/verify", json={"station_id": station_id, "check": check}).status_code == 200

    complete = client.post("/api/setup/complete", json={"station_id": station_id})

    assert complete.status_code == 200, complete.text
    cert = complete.json()["deployment_certificate"]
    assert cert["certified"] is True
    assert cert["status"] == "certified"
    assert cert["certificate_id"].startswith(f"RBW-{station_id}-")


def test_setup_repair_dependencies_reruns_bootstrap(client, monkeypatch):
    calls = {"count": 0}

    def _fake_bootstrap():
        calls["count"] += 1
        return {}

    _make_dependencies_ready(monkeypatch)
    _make_ai_ready(monkeypatch)
    monkeypatch.setattr("app.dependency_bootstrap.bootstrap_dependencies", _fake_bootstrap)

    res = client.post("/api/setup/repair-dependencies", json={"station_id": 1})

    assert res.status_code == 200, res.text
    assert calls["count"] == 1
