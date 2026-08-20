from app import config


def test_get_public_base_url_prefers_env(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://radio.example.com")
    assert config.get_public_base_url() == "https://radio.example.com"


def test_get_public_base_url_defaults_to_empty_string(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert config.get_public_base_url() == ""


def test_get_cors_origins_defaults_to_localhost_only(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert "http://localhost:8100" in config.get_cors_origins()


def test_get_cors_origins_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert config.get_cors_origins() == [
        "http://localhost:8100",
        "http://127.0.0.1:8100",
    ]


def test_proxy_and_security_flags_are_bool(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "1")
    assert config.get_trust_proxy_headers() is True
    assert config.get_security_headers_enabled() is True


def test_proxy_flag_accepts_explicit_false_token(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "off")
    assert config.get_trust_proxy_headers() is False


def test_security_headers_unknown_token_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SECURITY_HEADERS_ENABLED", "treu")
    assert config.get_security_headers_enabled() is True


def test_max_upload_bytes_defaults_and_parses_env(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    assert config.get_max_upload_bytes() == 512 * 1024 * 1024

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1048576")
    assert config.get_max_upload_bytes() == 1048576
