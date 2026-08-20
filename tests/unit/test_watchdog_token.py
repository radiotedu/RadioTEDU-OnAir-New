from __future__ import annotations

from types import SimpleNamespace

from app.security import watchdog_token


def _request(host: str, token: str = ""):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers={watchdog_token.WATCHDOG_HEADER: token} if token else {},
    )


def test_watchdog_token_is_stable_and_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog_token, "get_data_root", lambda: tmp_path)

    token = watchdog_token.ensure_watchdog_token()

    assert len(token) >= 32
    assert watchdog_token.ensure_watchdog_token() == token
    assert watchdog_token.watchdog_request_is_valid(_request("127.0.0.1", token)) is True
    assert watchdog_token.watchdog_request_is_valid(_request("::1", token)) is True
    assert watchdog_token.watchdog_request_is_valid(_request("192.0.2.20", token)) is False
    assert watchdog_token.watchdog_request_is_valid(_request("127.0.0.1", "wrong")) is False


def test_missing_local_token_creates_secret_but_does_not_authenticate(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog_token, "get_data_root", lambda: tmp_path)

    assert watchdog_token.watchdog_request_is_valid(_request("127.0.0.1")) is False
    assert watchdog_token.watchdog_token_path().is_file()
