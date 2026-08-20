from app.services import public_stream_evidence as evidence


def _audio_response(*_args, **_kwargs):
    return 206, "audio/ogg; charset=binary", b"OggS" * 64


def test_public_evidence_is_sanitized_and_uses_pinned_audio_evidence(monkeypatch):
    calls = {"http": 0}
    monkeypatch.setattr(evidence, "get_public_base_url", lambda: "https://public.example")
    monkeypatch.setattr(evidence, "_resolve_public_addresses", lambda *_args: ("203.0.113.8",))
    monkeypatch.setattr(evidence, "_tls_reachable", lambda *_args: True)

    def fake_fetch(*_args, **_kwargs):
        calls["http"] += 1
        return _audio_response()

    monkeypatch.setattr(evidence, "_fetch_pinned_audio", fake_fetch)
    service = evidence.PublicStreamEvidenceService()

    service._refresh(("https://public.example",))
    first = service.snapshot({})
    service._refresh(("https://public.example",))
    second = service.snapshot({})

    assert first["state"] == second["state"] == "healthy"
    assert first["streams"]["ai"]["dns"] == "reachable"
    assert first["streams"]["ai"]["tls"] == "reachable"
    assert first["streams"]["ai"]["http"] == "reachable"
    assert first["streams"]["ai"]["audio_bytes"] == "present"
    assert first["streams"]["ai"]["decode"] == "unknown"
    assert calls == {"http": 4}
    assert "public.example" not in repr(first)
    assert "https://" not in repr(first)


def test_invalid_or_unconfigured_origin_is_unknown_without_network(monkeypatch):
    monkeypatch.setattr(evidence, "get_public_base_url", lambda: "http://127.0.0.1:8000")
    calls = {"thread": 0}

    class _Thread:
        def __init__(self, *_args, **_kwargs):
            calls["thread"] += 1

        def start(self):
            raise AssertionError("unconfigured origins must not start a network probe")

    monkeypatch.setattr(evidence.threading, "Thread", _Thread)
    payload = evidence.PublicStreamEvidenceService().snapshot(
        {"stream_public_base_url": "https://user:secret@public.example"}
    )

    assert payload["state"] == "unknown"
    assert payload["configured"] is False
    assert payload["streams"]["ai"]["state"] == "unknown"
    assert calls == {"thread": 0}


def test_html_or_arbitrary_bytes_never_make_stream_healthy(monkeypatch):
    monkeypatch.setattr(evidence, "_resolve_public_addresses", lambda *_args: ("203.0.113.8",))
    monkeypatch.setattr(evidence, "_tls_reachable", lambda *_args: True)
    monkeypatch.setattr(
        evidence,
        "_fetch_pinned_audio",
        lambda *_args: (200, "text/html", b"<html>not audio</html>"),
    )

    item = evidence._probe_endpoint("https://public.example/ai", now_epoch=123)

    assert item["state"] == "degraded"
    assert item["audio_bytes"] == "unavailable"
    assert "public.example" not in repr(item)


def test_dns_rejects_private_or_mixed_answers_and_non_global_literals(monkeypatch):
    monkeypatch.setattr(
        evidence.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("203.0.113.8", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ],
    )

    assert evidence._resolve_public_addresses("rebind.example", 443) == ()
    assert evidence._safe_origin("https://100.64.0.1") is None
    assert evidence._safe_origin("https://224.0.0.1") is None


def test_pinned_probe_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(evidence, "_resolve_public_addresses", lambda *_args: ("203.0.113.8",))
    monkeypatch.setattr(evidence, "_tls_reachable", lambda *_args: True)
    monkeypatch.setattr(
        evidence,
        "_fetch_pinned_audio",
        lambda *_args: (302, "text/html", b""),
    )

    item = evidence._probe_endpoint("https://public.example/ai", now_epoch=123)

    assert item["state"] == "unavailable"
    assert item["http"] == "unavailable"


def test_cache_never_reuses_evidence_for_a_different_origin(monkeypatch):
    monkeypatch.setattr(evidence, "get_public_base_url", lambda: "")
    service = evidence.PublicStreamEvidenceService()
    service._cache = (
        evidence.time.monotonic(),
        ("https://first.example",),
        {"state": "healthy", "observed_at": 1, "streams": {}},
    )

    class _Thread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(evidence.threading, "Thread", _Thread)
    payload = service.snapshot({"stream_public_base_url": "https://second.example"})

    assert payload["state"] == "probing"
    assert payload["observed_at"] is None


def test_refresh_thread_start_failure_does_not_wedge_future_probes(monkeypatch):
    monkeypatch.setattr(evidence, "get_public_base_url", lambda: "https://public.example")
    attempts = []

    class _Thread:
        def __init__(self, *_args, **_kwargs):
            attempts.append(1)

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(evidence.threading, "Thread", _Thread)
    service = evidence.PublicStreamEvidenceService()

    service.snapshot({})
    service.snapshot({})

    assert len(attempts) == 2
