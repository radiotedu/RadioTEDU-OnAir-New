import json

from app.api.streaming import (
    StreamingFeatureSettingsUpdate,
    get_streaming_features,
    update_streaming_features,
)
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository


def test_streaming_admin_secrets_are_vaulted_and_never_echoed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cleanroom.db"
    vault_path = tmp_path / "streaming-credentials.json"
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(db_path))
    monkeypatch.setenv("CLEANROOM_CREDENTIAL_STORE_FILE", str(vault_path))
    init_db()

    update_streaming_features(
        StreamingFeatureSettingsUpdate(
            stream_public_base_url="https://stream.example.test",
            radio_website_url="https://radio.example.test",
            rocket_admin_user="operator",
            rocket_admin_password="admin-secret",
            rocket_health_password="health-secret",
        ),
        _user={},
    )

    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        assert settings["rocket_admin_password"].startswith(
            "credential://user/system/"
        )
        assert settings["rocket_health_password"].startswith(
            "credential://user/system/"
        )
    finally:
        conn.close()

    response = get_streaming_features(_user={})
    serialized = json.dumps(response, sort_keys=True)
    assert "admin-secret" not in serialized
    assert "health-secret" not in serialized
    assert response["system"]["rocket_admin_password_set"] is True
    assert response["system"]["rocket_health_password_set"] is True

    vault_raw = vault_path.read_text(encoding="utf-8")
    assert "admin-secret" not in vault_raw
    assert "health-secret" not in vault_raw


def test_blank_streaming_password_update_preserves_existing_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setenv(
        "CLEANROOM_CREDENTIAL_STORE_FILE",
        str(tmp_path / "streaming-credentials.json"),
    )
    init_db()
    initial = StreamingFeatureSettingsUpdate(
        rocket_admin_password="keep-me",
        rocket_health_password="keep-health",
    )
    update_streaming_features(initial, _user={})

    update_streaming_features(
        StreamingFeatureSettingsUpdate(
            rocket_admin_password="",
            rocket_health_password="",
        ),
        _user={},
    )

    response = get_streaming_features(_user={})
    assert response["system"]["rocket_admin_password_set"] is True
    assert response["system"]["rocket_health_password_set"] is True
