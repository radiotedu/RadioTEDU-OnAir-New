import json
import math
import socket
import threading
import time

import pytest


def test_schema_contains_high_assurance_tables():
    from app.db import get_connection, init_db

    init_db()
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {
        "audit_chain",
        "witness_audit_anchors",
        "ha_state",
        "replication_journal",
        "media_manifests",
        "recovery_points",
        "stream_config_drafts",
        "stream_config_operations",
        "guest_invites",
        "guest_sessions",
        "guest_recordings",
        "guest_recording_consents",
    } <= tables


def test_audit_chain_detects_tampering():
    from app.db import get_connection
    from app.services.audit_chain import audit_chain

    first = audit_chain.append(category="stream", action="draft", payload={"mount": "/safe"})
    audit_chain.append(category="stream", action="apply", payload={"draft": first["id"]})
    assert audit_chain.verify()["valid"] is True

    conn = get_connection()
    try:
        conn.execute("UPDATE audit_chain SET action='tampered' WHERE id=?", (first["id"],))
        conn.commit()
    finally:
        conn.close()
    verification = audit_chain.verify()
    assert verification["valid"] is False
    assert verification["failed_id"] == first["id"]


def test_guest_invite_is_single_redemption_and_admission_is_off_air(client):
    invite = client.post("/api/studios/1/guest-invites").json()
    token = invite["join_url"].split("#invite=", 1)[1]

    guest = client.post(
        "/api/guest/redeem",
        headers={"X-Test-No-Auto-Auth": "1"},
        json={"invite_token": token, "display_name": "Remote Guest"},
    )
    assert guest.status_code == 200
    session = guest.json()

    reused = client.post(
        "/api/guest/redeem",
        headers={"X-Test-No-Auto-Auth": "1"},
        json={"invite_token": token, "display_name": "Other"},
    )
    assert reused.status_code == 409

    admitted = client.post(f"/api/studios/1/guest-room/{session['session_id']}/admit")
    assert admitted.status_code == 200
    assert admitted.json()["status"] == "admitted"
    assert admitted.json()["is_on_air"] == 0

    taken = client.patch(
        f"/api/studios/1/guest-room/{session['session_id']}/audio",
        json={"on_air": True, "gain_db": 6},
    )
    assert taken.status_code == 200
    assert taken.json()["is_on_air"] == 1
    assert taken.json()["gain_db"] == 6


def test_guest_room_caps_admitted_guests_at_four(client):
    session_ids = []
    for index in range(5):
        invite = client.post("/api/studios/1/guest-invites").json()
        token = invite["join_url"].split("#invite=", 1)[1]
        redeemed = client.post(
            "/api/guest/redeem",
            headers={"X-Test-No-Auto-Auth": "1"},
            json={"invite_token": token, "display_name": f"Guest {index}"},
        )
        session_ids.append(redeemed.json()["session_id"])
    for session_id in session_ids[:4]:
        assert client.post(f"/api/studios/1/guest-room/{session_id}/admit").status_code == 200
    assert client.post(f"/api/studios/1/guest-room/{session_ids[4]}/admit").status_code == 409


class _FakeSocket:
    def close(self):
        pass


def test_stream_draft_validate_apply_and_idempotency(client, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: _FakeSocket())
    payload = {
        "station_id": 1,
        "icecast_enabled": True,
        "icecast_host": "stream.example.org",
        "icecast_port": 8000,
        "icecast_mount": "/safe",
        "icecast_user": "source",
        "icecast_password": "protected-secret",
        "stream_codec_profile": "mp3_128",
    }
    draft = client.post("/api/stream-config/drafts", json=payload)
    assert draft.status_code == 200, draft.text
    assert '"icecast_password":' not in json.dumps(draft.json())

    report = client.post(f"/api/stream-config/drafts/{draft.json()['id']}/validate")
    assert report.status_code == 200
    assert report.json()["outcome"] == "ready"

    headers = {"Idempotency-Key": "fixed-operation-key"}
    applied = client.post(f"/api/stream-config/drafts/{draft.json()['id']}/apply", headers=headers, json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    repeated = client.post(f"/api/stream-config/drafts/{draft.json()['id']}/apply", headers=headers, json={})
    assert repeated.status_code == 200
    assert repeated.json()["id"] == applied.json()["id"]

    saved = client.get("/api/stations/output?station_id=1").json()
    assert saved["icecast_mount"] == "/safe"
    assert saved["stream_codec_profile"] == "mp3_128"
    assert saved["icecast_password"] == ""
    assert saved["icecast_password_configured"] is True


def test_stream_apply_rejects_needs_attention(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(socket, "create_connection", unreachable)
    draft = client.post(
        "/api/stream-config/drafts",
        json={
            "station_id": 1,
            "icecast_host": "offline.invalid",
            "icecast_port": 8000,
            "icecast_mount": "/safe",
            "icecast_user": "source",
            "icecast_password": "secret",
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    report = client.post(f"/api/stream-config/drafts/{draft['id']}/validate").json()
    assert report["outcome"] == "needs_attention"
    response = client.post(
        f"/api/stream-config/drafts/{draft['id']}/apply",
        headers={"Idempotency-Key": "unsafe-operation"},
        json={},
    )
    assert response.status_code == 409


class _RunningStreamRuntime:
    def __init__(self):
        self.starts = []

    def status(self, station_id):
        return {
            "station_id": station_id,
            "running": True,
            "output_feed_active": True,
            "active_input_uri": "C:/media/current.mp3",
            "stream_title": "Current",
            "stream_artist": "Artist",
            "track_type": "music",
        }

    def start_station(self, station_id, input_uri, **kwargs):
        self.starts.append((station_id, input_uri, kwargs))
        return self.status(station_id)


def test_live_stream_apply_requires_listener_audio_bytes(client, monkeypatch):
    from types import SimpleNamespace

    from app.api import runtime as runtime_api
    from app.services import stream_config_service as stream_service

    runtime = _RunningStreamRuntime()
    monkeypatch.setattr(runtime_api, "runtime_registry", runtime)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: _FakeSocket())
    monkeypatch.setattr(
        stream_service,
        "probe_configured_audio",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True),
    )
    monkeypatch.setenv("CLEANROOM_STREAM_VERIFY_SECONDS", "1")

    draft = client.post(
        "/api/stream-config/drafts",
        json={
            "station_id": 1,
            "icecast_host": "stream.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/verified",
            "icecast_user": "source",
            "icecast_password": "protected-secret",
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    report = client.post(
        f"/api/stream-config/drafts/{draft['id']}/validate"
    ).json()
    assert report["outcome"] == "ready"
    applied = client.post(
        f"/api/stream-config/drafts/{draft['id']}/apply",
        headers={"Idempotency-Key": "listener-audio-success"},
        json={},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["result"]["live_output_verified"] is True
    assert applied.json()["result"]["listener_audio_verified"] is True
    assert runtime.starts


def test_live_stream_apply_rolls_back_when_listener_payload_is_empty(
    client,
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import runtime as runtime_api
    from app.services import stream_config_service as stream_service

    before = client.get("/api/stations/output?station_id=1").json()
    runtime = _RunningStreamRuntime()
    monkeypatch.setattr(runtime_api, "runtime_registry", runtime)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: _FakeSocket())
    monkeypatch.setattr(
        stream_service,
        "probe_configured_audio",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False),
    )
    monkeypatch.setenv("CLEANROOM_STREAM_VERIFY_SECONDS", "1")

    draft = client.post(
        "/api/stream-config/drafts",
        json={
            "station_id": 1,
            "icecast_host": "silent.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/silent",
            "icecast_user": "source",
            "icecast_password": "protected-secret",
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    report = client.post(
        f"/api/stream-config/drafts/{draft['id']}/validate"
    ).json()
    assert report["outcome"] == "ready"
    applied = client.post(
        f"/api/stream-config/drafts/{draft['id']}/apply",
        headers={"Idempotency-Key": "listener-audio-empty"},
        json={},
    )
    assert applied.status_code == 400
    assert "listener_audio_verification_failed" in applied.text
    after = client.get("/api/stations/output?station_id=1").json()
    assert after["icecast_host"] == before["icecast_host"], (before, after)
    assert after["icecast_mount"] == before["icecast_mount"], (before, after)
    from app.db import get_connection

    conn = get_connection()
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM station_outputs WHERE station_id=1"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_live_stream_apply_can_save_and_retry_when_listener_is_unavailable(
    client,
    monkeypatch,
):
    from app.api import runtime as runtime_api
    from app.services import stream_config_service as stream_service

    runtime = _RunningStreamRuntime()
    monkeypatch.setattr(runtime_api, "runtime_registry", runtime)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("destination unavailable")
        ),
    )
    monkeypatch.setattr(
        stream_service,
        "probe_configured_audio",
        lambda *_args, **_kwargs: pytest.fail(
            "deferred listener verification must not block configuration save"
        ),
    )

    draft = client.post(
        "/api/stream-config/drafts",
        json={
            "station_id": 1,
            "icecast_host": "stream.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/classic",
            "icecast_user": "source",
            "icecast_password": "protected-secret",
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    report = client.post(
        f"/api/stream-config/drafts/{draft['id']}/validate"
    ).json()
    assert report["outcome"] == "needs_attention"

    applied = client.post(
        f"/api/stream-config/drafts/{draft['id']}/apply",
        headers={"Idempotency-Key": "listener-audio-deferred"},
        json={"defer_listener_verification": True},
    )
    assert applied.status_code == 200, applied.text
    operation = applied.json()
    assert operation["status"] == "applied"
    assert operation["result"]["outcome"] == "needs_attention"
    assert operation["result"]["listener_verification_deferred"] is True
    assert operation["result"]["listener_audio_verified"] is False
    assert runtime.starts

    saved = client.get("/api/stations/output?station_id=1").json()
    assert saved["icecast_mount"] == "/classic"
    assert saved["icecast_password_configured"] is True


def test_failed_live_stream_apply_restores_output_and_mirrored_settings(
    client,
    monkeypatch,
):
    from types import SimpleNamespace

    from app.api import runtime as runtime_api
    from app.services import stream_config_service as stream_service

    original = {
        "station_id": 1,
        "local_output_enabled": False,
        "output_device_id": "",
        "icecast_enabled": True,
        "icecast_host": "original.example.org",
        "icecast_port": 8443,
        "icecast_mount": "/original",
        "icecast_user": "source",
        "icecast_password": "original-protected-secret",
        "icecast_tls_enabled": True,
        "output_gain_db": -3,
        "stream_codec_profile": "aac_plus_196",
        "stream_bitrate_kbps": 196,
    }
    saved = client.post("/api/stations/output", json=original)
    assert saved.status_code == 200, saved.text
    before_output = client.get("/api/stations/output?station_id=1").json()
    before_settings = client.get("/api/settings/station?station_id=1").json()[
        "settings"
    ]

    runtime = _RunningStreamRuntime()
    monkeypatch.setattr(runtime_api, "runtime_registry", runtime)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: _FakeSocket())
    monkeypatch.setattr(
        stream_service,
        "probe_configured_audio",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False),
    )
    monkeypatch.setenv("CLEANROOM_STREAM_VERIFY_SECONDS", "1")

    draft = client.post(
        "/api/stream-config/drafts",
        json={
            **original,
            "icecast_host": "silent.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/silent",
            "icecast_password": "replacement-protected-secret",
            "icecast_tls_enabled": False,
            "output_gain_db": 2,
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    assert client.post(
        f"/api/stream-config/drafts/{draft['id']}/validate"
    ).json()["outcome"] == "ready"
    applied = client.post(
        f"/api/stream-config/drafts/{draft['id']}/apply",
        headers={"Idempotency-Key": "listener-audio-existing-rollback"},
        json={},
    )
    assert applied.status_code == 400

    after_output = client.get("/api/stations/output?station_id=1").json()
    after_settings = client.get("/api/settings/station?station_id=1").json()[
        "settings"
    ]
    for key in (
        "icecast_host",
        "icecast_port",
        "icecast_mount",
        "icecast_user",
        "icecast_tls_enabled",
        "output_gain_db",
        "stream_codec_profile",
        "stream_bitrate_kbps",
    ):
        assert after_output[key] == before_output[key]
    for key in stream_service._OUTPUT_SETTING_KEYS:
        assert after_settings.get(key) == before_settings.get(key)


def test_stream_validation_blocks_reported_mount_conflict(client, monkeypatch):
    import urllib.request

    class NetworkSocket(_FakeSocket):
        def getpeername(self):
            return ("203.0.113.10", 8000)

    class StatusResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"icestats": {"source": {"listenurl": "http://stream.example.org:8000/conflict"}}}).encode()

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: NetworkSocket())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: StatusResponse())
    draft = client.post(
        "/api/stream-config/drafts",
        json={
            "station_id": 1,
            "icecast_host": "stream.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/conflict",
            "icecast_user": "source",
            "icecast_password": "secret",
            "stream_codec_profile": "mp3_128",
        },
    ).json()
    report = client.post(f"/api/stream-config/drafts/{draft['id']}/validate").json()
    assert report["outcome"] == "unsafe"
    assert report["checks"]["mount_conflict"]["status"] == "unsafe"


class _PcmSession:
    def __init__(self, sample):
        self.sample = int(sample)

    def read_pcm(self, size):
        return self.sample.to_bytes(2, "little", signed=True) * (size // 2)

    def snapshot(self):
        return {"level_db": -6.0, "peak_db": -3.0, "running": True}


def test_guest_audio_mixer_applies_gain_and_mix_minus():
    from app.audio.guest_audio_registry import GuestAudioRegistry
    from app.db import get_connection, init_db

    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO guest_invites(studio_id, station_id, token_hash, created_by, expires_at) VALUES (1,1,'x',1,'2999-01-01T00:00:00+00:00')")
        invite_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO guest_sessions(invite_id, studio_id, station_id, display_name, session_token_hash, status, is_connected, is_on_air, gain_db) VALUES (?,1,1,'Guest','y','admitted',1,1,6)",
            (invite_id,),
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    registry = GuestAudioRegistry()
    registry.register(session_id, _PcmSession(1000))
    mixed = registry.read_on_air_pcm(1, 8)
    expected = round(1000 * math.pow(10, 6 / 20))
    assert int.from_bytes(mixed[:2], "little", signed=True) == expected

    program = (expected + 4000).to_bytes(2, "little", signed=True) * 2 * 2
    registry.publish_program_pcm(1, program)
    returned = registry.return_buffer(session_id).read(len(program))
    assert abs(int.from_bytes(returned[:2], "little", signed=True) - 4000) <= 1


def test_ha_voter_refuses_split_brain_during_valid_lease(monkeypatch):
    from app.db import get_connection, init_db
    from app.services.ha_coordinator import HaCoordinator

    monkeypatch.setenv("CLEANROOM_HA_ENABLED", "1")
    monkeypatch.setenv("CLEANROOM_HA_NODE_ID", "witness")
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE ha_state SET current_term=7, voted_for='', leader_id='node-a', leader_lease_expires_at=? WHERE id=1",
            (time.time() + 30,),
        )
        conn.commit()
    finally:
        conn.close()

    coordinator = HaCoordinator()
    assert coordinator.grant_vote(8, "node-b") is False

    conn = get_connection()
    try:
        conn.execute("UPDATE ha_state SET leader_lease_expires_at=0 WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    assert coordinator.grant_vote(8, "node-b") is True
    assert coordinator.grant_vote(8, "node-c") is False


def test_ha_rejects_expired_heartbeat(monkeypatch):
    from app.services.ha_coordinator import HaCoordinator

    monkeypatch.setenv("CLEANROOM_HA_ENABLED", "1")
    monkeypatch.setenv("CLEANROOM_HA_NODE_ID", "node-b")
    coordinator = HaCoordinator()
    assert coordinator.receive_heartbeat(1, "node-a", time.time() - 1) is False
    assert coordinator.receive_heartbeat(1, "node-a", time.time() + 6) is True
    snapshot = coordinator.snapshot()
    assert snapshot["role"] == "follower"
    assert snapshot["leader_id"] == "node-a"
    assert snapshot["safe_to_broadcast"] is False


def test_ha_internal_credentials_expire(monkeypatch):
    from app.services.ha_coordinator import ha_token, validate_ha_token

    monkeypatch.setenv("CLEANROOM_HA_SHARED_SECRET", "test-shared-secret")
    assert validate_ha_token(ha_token()) is True
    assert validate_ha_token(ha_token(time.time() - 120)) is False


def test_independent_lease_watchdog_fences_stalled_leader(monkeypatch):
    from app.services.ha_coordinator import HaCoordinator

    monkeypatch.setenv("CLEANROOM_HA_ENABLED", "1")
    monkeypatch.setenv("CLEANROOM_HA_NODE_ID", "node-a")
    coordinator = HaCoordinator()
    fenced = threading.Event()
    coordinator.register_role_callback(lambda role, _snapshot: fenced.set() if role == "follower" else None)
    coordinator._lease_deadline_monotonic = time.monotonic() + 0.05
    coordinator._set_role("leader", quorum=True, leader_id="node-a")
    watchdog = threading.Thread(target=coordinator._watchdog_loop, daemon=True)
    watchdog.start()
    try:
        assert fenced.wait(1.0)
        assert coordinator.snapshot()["safe_to_broadcast"] is False
        assert coordinator.snapshot()["role"] == "follower"
    finally:
        coordinator._stop.set()
        watchdog.join(timeout=1)


def test_witness_persists_and_signs_audit_anchor(monkeypatch):
    from app.api.ha import AnchorPayload, ha_audit_anchor
    from app.db import get_connection, init_db
    from app.services.ha_coordinator import ha_coordinator, ha_token

    monkeypatch.setenv("CLEANROOM_HA_SHARED_SECRET", "test-shared-secret")
    monkeypatch.setattr(ha_coordinator, "witness_only", True)
    init_db()
    result = ha_audit_anchor(
        AnchorPayload(entry_hash="a" * 64, node_id="node-a"),
        x_onair_ha_token=ha_token(),
    )
    assert result["accepted"] is True
    assert result["witness"] is True
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM witness_audit_anchors WHERE entry_hash=?", ("a" * 64,)).fetchone()
    finally:
        conn.close()
    assert row["node_id"] == "node-a"
    assert row["signature"] == result["signature"]


def test_one_hundred_witnessed_failover_cycles_never_grant_two_leases(monkeypatch):
    from app.db import get_connection, init_db
    from app.services.ha_coordinator import HaCoordinator

    monkeypatch.setenv("CLEANROOM_HA_ENABLED", "1")
    monkeypatch.setenv("CLEANROOM_HA_NODE_ID", "witness")
    init_db()
    coordinator = HaCoordinator()
    for cycle in range(1, 101):
        candidate = "node-a" if cycle % 2 else "node-b"
        other = "node-b" if candidate == "node-a" else "node-a"
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE ha_state SET current_term=?, voted_for='', leader_id='', leader_lease_expires_at=0 WHERE id=1",
                (cycle,),
            )
            conn.commit()
        finally:
            conn.close()
        assert coordinator.grant_vote(cycle, candidate) is True
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE ha_state SET leader_id=?, leader_lease_expires_at=? WHERE id=1",
                (candidate, time.time() + 6),
            )
            conn.commit()
        finally:
            conn.close()
        assert coordinator.grant_vote(cycle + 1, other) is False


def test_media_mirror_hashes_verifies_and_promotes(tmp_path):
    from app.services.media_mirror import MediaMirrorService

    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "jingles").mkdir(parents=True)
    (source / "jingles" / "id.wav").write_bytes(b"station-id")
    service = MediaMirrorService()
    expected = service.manifest(source)
    result = service.synchronize(source, target)
    assert result["ready"] is True
    assert result["actual"]["manifest_hash"] == expected["manifest_hash"]

    (target / "jingles" / "id.wav").write_bytes(b"corrupt")
    comparison = service.compare(expected, target)
    assert comparison["ready"] is False
    assert comparison["changed"] == ["jingles/id.wav"]


def test_acknowledgeable_schedule_mutation_is_journaled_with_checksum():
    from app.db import get_connection, init_db
    from app.repositories.schedule_repo import ScheduleRepository

    init_db()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO tracks(station_id, title, artist, track_type, file_path, duration, is_active) VALUES (1, 'Scheduled', 'Test', 'music', '', 60, 1)"
        )
        track_id = int(cur.lastrowid)
        schedule_id = ScheduleRepository(conn).enqueue(
            station_id=1,
            track_id=track_id,
            play_at="2099-01-01T00:00:00Z",
        )
    finally:
        conn.close()
    conn = get_connection()
    try:
        journal = conn.execute(
            "SELECT * FROM replication_journal WHERE entity_type='schedule' AND entity_id=?",
            (str(schedule_id),),
        ).fetchone()
    finally:
        conn.close()
    assert journal is not None
    assert len(str(journal["checksum"])) == 64


def test_recording_writes_primary_and_independent_mirror_without_blocking_audio(tmp_path, monkeypatch):
    import app.services.program_recording as recording_module

    created = []

    class FakeRecorder:
        def __init__(self, path):
            self.path = path
            self.chunks = []
            self.fail = False
            created.append(self)

        def push(self, chunk):
            self.chunks.append(bytes(chunk))
            return not self.fail

        def stop(self):
            return {"path": str(self.path), "failed": "", "returncode": 0}

    monkeypatch.setattr(recording_module, "_Recorder", FakeRecorder)
    monkeypatch.setenv("CLEANROOM_RECORDING_MIRROR_ROOT", str(tmp_path / "mirror"))
    service = recording_module.ProgramRecordingService()
    recording = service.request(1, actor_id=1)
    assert recording["status"] == "recording"
    assert recording["manifest"]["mirror_status"] == "ready"
    assert len(created) == 2

    service.publish_pcm(1, b"pcm")
    assert created[0].chunks == [b"pcm"]
    assert created[1].chunks == [b"pcm"]

    stopped = threading.Event()

    def slow_stop(recording_id, *, reason=""):
        time.sleep(0.2)
        stopped.set()
        return {"id": recording_id, "reason": reason}

    monkeypatch.setattr(service, "stop", slow_stop)
    created[0].fail = True
    started = time.monotonic()
    service.publish_pcm(1, b"next")
    assert time.monotonic() - started < 0.1
    assert stopped.wait(1)


def test_recording_deletion_removes_primary_and_mirror(tmp_path, monkeypatch):
    from app.db import get_connection, init_db
    from app.services.program_recording import ProgramRecordingService

    local_root = tmp_path / "local"
    mirror_root = tmp_path / "mirror"
    primary = local_root / "recordings" / "1" / "segment.flac"
    mirror = mirror_root / "1" / "segment.flac"
    primary.parent.mkdir(parents=True)
    mirror.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    mirror.write_bytes(b"mirror")
    monkeypatch.setenv("CLEANROOM_RECORDING_MIRROR_ROOT", str(mirror_root))
    import app.services.program_recording as recording_module
    monkeypatch.setattr(recording_module, "get_data_dir", lambda: local_root)
    init_db()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO guest_recordings(studio_id, station_id, status, manifest_json, file_path, started_by) VALUES (1,1,'completed',?,?,1)",
            (json.dumps({"segments": [{"path": str(primary), "mirror_paths": [str(mirror)]}]}), str(primary)),
        )
        recording_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    result = ProgramRecordingService().delete(recording_id, actor_id=1)
    assert result["files_removed"] == 2
    assert not primary.exists()
    assert not mirror.exists()


def test_standby_guest_snapshot_is_always_disconnected_and_off_air():
    from app.db import get_connection, init_db
    from app.services.replication_applier import replication_applier
    from app.services.replication_journal import replication_journal

    init_db()
    payload = {
        "studio_id": 1,
        "invites": [{
            "id": 9001,
            "studio_id": 1,
            "station_id": 1,
            "token_hash": "replicated-invite-hash",
            "created_by": 1,
            "expires_at": "2999-01-01T00:00:00+00:00",
            "redeemed_at": "2026-01-01T00:00:00+00:00",
            "revoked_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }],
        "sessions": [{
            "id": 9002,
            "invite_id": 9001,
            "studio_id": 1,
            "station_id": 1,
            "display_name": "Replicated Guest",
            "session_token_hash": "replicated-session-hash",
            "status": "admitted",
            "is_connected": 1,
            "is_muted": 0,
            "is_on_air": 1,
            "gain_db": 3,
            "connection_quality": "good",
            "admitted_at": "2026-01-01T00:00:00+00:00",
            "left_at": None,
            "last_seen_at": "2026-01-01T00:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
        }],
    }
    replication_journal.append("guest_room", 1, "snapshot", payload)
    assert replication_applier.apply_pending()["applied"] >= 1

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM guest_sessions WHERE id=9002").fetchone()
    finally:
        conn.close()
    assert row["status"] == "admitted"
    assert row["is_connected"] == 0
    assert row["is_on_air"] == 0


def test_basic_stream_operator_can_change_preset_but_not_destination():
    from app.db import get_connection, init_db
    from app.repositories.settings_repo import SettingsRepository
    from app.repositories.station_output_repo import StationOutputRepository
    from app.services.stream_config_service import StreamConfigError, StreamConfigService

    init_db()
    conn = get_connection()
    try:
        StationOutputRepository(conn).upsert(
            station_id=1,
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            icecast_host="saved.example.org",
            icecast_port=8000,
            icecast_mount="/saved",
            icecast_user="source",
            icecast_password="",
            output_gain_db=0,
            stream_codec_profile="mp3_128",
            stream_bitrate_kbps=128,
        )
        SettingsRepository(conn).upsert_station(1, {"icecast_tls_enabled": "false"})
    finally:
        conn.close()
    base = {
        "station_id": 1,
        "icecast_host": "saved.example.org",
        "icecast_port": 8000,
        "icecast_mount": "/saved",
        "icecast_user": "source",
        "stream_codec_profile": "aac_plus_196",
    }
    service = StreamConfigService()
    assert service.create_draft(base, actor_id=1, allow_advanced=False)["config"]["stream_codec_profile"] == "aac_plus_196"
    with pytest.raises(StreamConfigError, match="advanced_permission_required_for_destination"):
        service.create_draft({**base, "icecast_host": "new.example.org"}, actor_id=1, allow_advanced=False)


def test_basic_stream_operator_can_defer_listener_check_for_preset_change(
    client,
    dj_token_headers,
    monkeypatch,
):
    saved = client.post(
        "/api/stations/output",
        json={
            "station_id": 1,
            "icecast_enabled": True,
            "icecast_host": "saved.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/saved",
            "icecast_user": "source",
            "icecast_password": "protected-secret",
            "stream_codec_profile": "mp3_128",
        },
    )
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: _FakeSocket())

    draft = client.post(
        "/api/stream-config/drafts",
        headers=dj_token_headers,
        json={
            "station_id": 1,
            "icecast_host": "saved.example.org",
            "icecast_port": 8000,
            "icecast_mount": "/saved",
            "icecast_user": "source",
            "icecast_password": "",
            "stream_codec_profile": "aac_plus_196",
        },
    )
    assert draft.status_code == 200, draft.text
    draft_id = draft.json()["id"]
    validated = client.post(
        f"/api/stream-config/drafts/{draft_id}/validate",
        headers=dj_token_headers,
    )
    assert validated.status_code == 200, validated.text
    applied = client.post(
        f"/api/stream-config/drafts/{draft_id}/apply",
        headers={
            **dj_token_headers,
            "Idempotency-Key": "basic-listener-deferred",
        },
        json={"defer_listener_verification": True},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"

    output = client.get("/api/stations/output?station_id=1").json()
    assert output["stream_codec_profile"] == "aac_plus_196"
    assert output["icecast_password_configured"] is True


def test_stream_wizard_markup_is_plain_language_and_accessible():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    html = (root / "app" / "static" / "onair" / "index.html").read_text(encoding="utf-8")
    script = (root / "app" / "static" / "onair" / "app.js").read_text(encoding="utf-8")
    for step in range(1, 5):
        assert f"STEP {step} OF 4" in html
    assert "Opus Normal — 192 kbps" in html
    assert "every music station adds Opus 32 Low" in html
    assert "only Classical plus Cazz add lossless FLAC" in html
    assert "Opus Normal — 96 kbps" not in html
    assert "Opus High — 192 kbps" not in html
    assert "MP3 128 kbps" not in html
    assert "Review what listeners will hear" in html
    assert '<details id="streamAdvancedSettings">' in html
    assert 'id="streamWizardChecks"' in html and 'aria-live="polite"' in html
    assert ">Apply safely</button>" in html
    assert 'id="reloadBackendButton"' in html
    assert "Ready: every required pre-apply check passed" in script
    assert "Needs attention:" in script
    assert "Unsafe to apply:" in script
    assert "defer_listener_verification: true" in script
    assert "timeoutMs: 30000" in script
    assert "Saved immediately" in script
    assert "the listener received audio bytes" in script
    assert "Listener audio will be verified automatically" in script
    assert "/api/maintenance/backend/reload" in script
    assert "previous_backend_instance_id" in script
