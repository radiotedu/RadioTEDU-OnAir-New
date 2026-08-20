from app import config


def test_webrtc_enabled_defaults_to_true(monkeypatch):
    monkeypatch.delenv("WEBRTC_ENABLED", raising=False)
    monkeypatch.setattr(config, "_webrtc_runtime_available", lambda: True)
    assert config.get_webrtc_enabled() is True


def test_webrtc_enabled_respects_false(monkeypatch):
    monkeypatch.setenv("WEBRTC_ENABLED", "false")
    assert config.get_webrtc_enabled() is False


def test_webrtc_stun_url_defaults(monkeypatch):
    monkeypatch.delenv("WEBRTC_STUN_URL", raising=False)
    assert config.get_webrtc_stun_url() == "stun:stun.l.google.com:19302"


def test_webrtc_stun_url_from_env(monkeypatch):
    monkeypatch.setenv("WEBRTC_STUN_URL", "stun:my.stun:3478")
    assert config.get_webrtc_stun_url() == "stun:my.stun:3478"


def test_webrtc_turn_url_defaults_empty(monkeypatch):
    monkeypatch.delenv("WEBRTC_TURN_URL", raising=False)
    assert config.get_webrtc_turn_url() == ""


def test_webrtc_turn_credentials_from_env(monkeypatch):
    monkeypatch.setenv("WEBRTC_TURN_URL", "turn:turn.example.com:3478")
    monkeypatch.setenv("WEBRTC_TURN_USERNAME", "user1")
    monkeypatch.setenv("WEBRTC_TURN_CREDENTIAL", "pass1")
    assert config.get_webrtc_turn_url() == "turn:turn.example.com:3478"
    assert config.get_webrtc_turn_username() == "user1"
    assert config.get_webrtc_turn_credential() == "pass1"


def test_webrtc_ice_servers_stun_only(monkeypatch):
    monkeypatch.delenv("WEBRTC_TURN_URL", raising=False)
    monkeypatch.delenv("WEBRTC_TURN_USERNAME", raising=False)
    monkeypatch.delenv("WEBRTC_TURN_CREDENTIAL", raising=False)
    servers = config.get_webrtc_ice_servers()
    assert len(servers) == 1
    assert servers[0]["urls"] == "stun:stun.l.google.com:19302"


def test_webrtc_ice_servers_includes_turn_when_configured(monkeypatch):
    monkeypatch.setenv("WEBRTC_TURN_URL", "turn:t.example.com:3478")
    monkeypatch.setenv("WEBRTC_TURN_USERNAME", "u")
    monkeypatch.setenv("WEBRTC_TURN_CREDENTIAL", "p")
    servers = config.get_webrtc_ice_servers()
    assert len(servers) == 2
    turn = servers[1]
    assert turn["urls"] == "turn:t.example.com:3478"
    assert turn["username"] == "u"
    assert turn["credential"] == "p"


def test_webrtc_enabled_false_when_aiortc_missing(monkeypatch):
    monkeypatch.delenv("WEBRTC_ENABLED", raising=False)
    monkeypatch.setattr(config, "_webrtc_runtime_available", lambda: False)
    assert config.get_webrtc_enabled() is False
