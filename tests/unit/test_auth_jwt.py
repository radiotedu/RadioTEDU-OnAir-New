import importlib

import app.auth.jwt_handler as jwt_handler


def test_access_token_contains_subject_and_role(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "secret-123")

    importlib.reload(jwt_handler)

    token = jwt_handler.create_access_token(user_id=7, role="dj")
    payload = jwt_handler.decode_token(token)

    assert payload["sub"] == "7"
    assert payload["role"] == "dj"
