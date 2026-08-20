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


def test_hls_defaults_to_planned_he_aac_192_and_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()

    payload = get_hls_settings(_user={})

    assert payload == {
        "enabled": False,
        "runtime_available": False,
        "status": "planned",
        "codec_profile": "he_aac_192",
        "codec": "HE-AAC",
        "bitrate_kbps": 192,
        "playlist_active": False,
        "stored_disabled": True,
    }


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
        assert settings["hls_codec_profile"] == "he_aac_192"
        assert settings["hls_bitrate_kbps"] == "192"
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
    assert "HE-AAC · 192 kbps" in html
    assert 'id="hlsEnabled" type="checkbox" disabled' in html
    assert "HLS is not active." in html
    assert 'id="rocketHlsEnabled"' not in html
    assert "api('/api/settings/hls'" in script
    assert "JSON.stringify({ enabled: false, codec_profile: 'he_aac_192' })" in script
