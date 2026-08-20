from app.launcher import wait_for_health


def test_wait_for_health_returns_true_after_probe_succeeds(monkeypatch):
    class FakeResponse:
        status = 200

    calls = {"count": 0}

    def fake_probe(_url, _timeout):
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError("not ready")
        return FakeResponse()

    monkeypatch.setattr("app.launcher.probe_url", fake_probe)

    assert (
        wait_for_health(
            "http://127.0.0.1:8100/api/health/ready",
            retries=5,
            delay_sec=0.01,
        )
        is True
    )
