import json

import pytest
from fastapi import HTTPException

import app.api.integrations as integrations
from app.api.integrations import (
    PublishVotingRoundPayload,
    RadioTEDUIntegrationSettingsUpdate,
    VotingCandidate,
)
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository


def _payload() -> PublishVotingRoundPayload:
    return PublishVotingRoundPayload(
        round_id="round-2026",
        candidates=[
            VotingCandidate(
                id=f"candidate-{index}",
                song_id=f"song-{index}",
                title=f"Song {index}",
                artist="RadioTEDU",
            )
            for index in range(1, 4)
        ],
    )


def test_voting_adapter_matches_agent_contract_and_vaults_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    vault_path = tmp_path / "credentials.json"
    monkeypatch.setenv("CLEANROOM_CREDENTIAL_STORE_FILE", str(vault_path))
    init_db()
    integrations.update_radiotedu_integrations(
        RadioTEDUIntegrationSettingsUpdate(
            voting_enabled=True,
            voting_base_url="http://127.0.0.1:3030/api/v1",
            voting_agent_device_id="onair-studio-a",
            voting_agent_token="agent-secret",
            study_enabled=True,
            study_base_url="https://radiotedu.com/jukebox/api/v1/study",
        ),
        _user={},
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return {"ok": True, "status": 200, "data": {"data": {"round": {}}}}

    monkeypatch.setattr(integrations, "_request_json", fake_request)
    result = integrations.publish_voting_round(_payload(), _user={})

    assert result["state"] == "published"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith(
        "/next-song-voting/agent/rounds"
    )
    assert calls[0]["token"] == "agent-secret"
    assert calls[0]["device_id"] == "onair-studio-a"
    outbound = calls[0]["payload"]
    assert outbound["id"] == "round-2026"
    assert len(outbound["candidates"]) == 3
    assert set(outbound["candidates"][0]) == {
        "id",
        "songId",
        "title",
        "artist",
        "albumArtUrl",
    }

    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        assert settings["radiotedu_voting_agent_token"].startswith(
            "credential://user/system/"
        )
    finally:
        conn.close()
    assert "agent-secret" not in vault_path.read_text(encoding="utf-8")
    assert "agent-secret" not in json.dumps(
        integrations.get_radiotedu_integrations(_user={})
    )


def test_optional_service_outage_is_explicit_and_never_affects_playout(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE",
        str(tmp_path / "credentials.json"),
    )
    init_db()
    integrations.update_radiotedu_integrations(
        RadioTEDUIntegrationSettingsUpdate(
            voting_enabled=True,
            voting_base_url="http://127.0.0.1:3030/api/v1",
            voting_agent_device_id="test-agent",
            voting_agent_token="test-token",
        ),
        _user={},
    )
    monkeypatch.setattr(
        integrations,
        "_request_json",
        lambda *args, **kwargs: {
            "ok": False,
            "status": 0,
            "error_code": "remote_unavailable",
            "message": "Core playout is unaffected.",
        },
    )

    result = integrations.publish_voting_round(_payload(), _user={})

    assert result["state"] == "degraded"
    assert result["core_playout_affected"] is False
    assert result["result"]["error_code"] == "remote_unavailable"


def test_external_integration_requires_https():
    with pytest.raises(HTTPException) as exc:
        integrations._validated_base_url(
            "http://radiotedu.example/api/v1",
            setting="voting_base_url",
        )
    assert exc.value.status_code == 400
