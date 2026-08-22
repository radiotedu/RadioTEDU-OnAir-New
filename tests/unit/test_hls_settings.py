from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.streaming import (
    HlsSettingsUpdate,
    StreamingFeatureSettingsUpdate,
    get_hls_settings,
    update_hls_settings,
    update_streaming_features,
)
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository


def test_hls_defaults_to_stopped_he_aac_v1_96_192_and_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    payload = get_hls_settings(_user={})

    assert payload["enabled"] is False
    assert payload["runtime_available"] is False
    assert payload["status"] == "stopped"
    assert payload["codec_profile"] == "he_aac_v1_96_192"
    assert payload["codec"] == "HE-AAC v1"
    assert payload["low_bitrate_kbps"] == 96
    assert payload["high_bitrate_kbps"] == 192
    assert payload["playlist_active"] is False
    assert payload["stored_disabled"] is True
    assert payload["encoder"] == "libfdk_aac"
    assert payload["credentials_exposed"] is False


def test_hls_disabled_policy_is_persisted_and_true_requests_fail_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    saved = update_hls_settings(HlsSettingsUpdate(), _user={})
    assert saved["ok"] is True
    assert saved["hls"]["enabled"] is False

    conn = get_connection()
    try:
        settings = SettingsRepository(conn).get_system()
        assert settings["hls_enabled"] == "false"
        assert settings["rocket_hls_enabled"] == "false"
        assert settings["hls_codec_profile"] == "he_aac_v1_96_192"
        assert settings["hls_bitrate_kbps"] == "192"
        assert settings["hls_low_bitrate_kbps"] == "96"
        assert settings["hls_high_bitrate_kbps"] == "192"
    finally:
        conn.close()

    with pytest.raises(HTTPException) as exc_info:
        update_hls_settings(HlsSettingsUpdate(enabled=True), _user={})
    assert exc_info.value.status_code == 409

    with pytest.raises(HTTPException) as legacy_exc:
        update_streaming_features(
            StreamingFeatureSettingsUpdate(rocket_hls_enabled=True),
            _user={},
        )
    assert legacy_exc.value.status_code == 409


def test_hls_has_a_dedicated_settings_section_and_no_origin_toggle():
    root = Path(__file__).resolve().parents[2]
    html = (root / "app" / "static" / "onair" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (root / "app" / "static" / "onair" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'id="hlsSettingsPanel"' in html
    assert 'data-operator-view="settings"' in html
    assert "HE-AAC v1</strong> Low 96 / High 192 kbit/s" in html
    assert 'id="hlsEnabled" type="checkbox" disabled' in html
    assert "HLS ayrı bir katmandır." in html
    assert 'id="rocketHlsEnabled"' not in html
    assert "api('/api/settings/hls'" in script
    assert "api('/api/settings/hls/start'" in script
    assert "api('/api/settings/hls/stop'" in script
    assert "startHlsHomeButton" in script
