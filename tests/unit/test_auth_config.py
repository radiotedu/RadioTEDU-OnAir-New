import importlib

import app.config as config


def test_auth_config_reads_secret_and_cors_from_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "secret-123")
    monkeypatch.setenv(
        "CORS_ORIGINS", "https://radio.example.com,http://localhost:8100"
    )

    importlib.reload(config)

    assert config.get_jwt_secret_key() == "secret-123"
    assert config.get_cors_origins() == [
        "https://radio.example.com",
        "http://localhost:8100",
    ]


def test_auth_config_generates_and_persists_an_unpublished_secret(monkeypatch, tmp_path):
    secret_path = tmp_path / "runtime.jwt-secret"
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.setenv("CLEANROOM_JWT_SECRET_FILE", str(secret_path))

    first = config.get_jwt_secret_key()
    second = config.get_jwt_secret_key()

    assert len(first) >= 48
    assert second == first
    assert secret_path.read_text(encoding="utf-8") == first
    assert first != "cleanroom-dev-secret-change-me"
