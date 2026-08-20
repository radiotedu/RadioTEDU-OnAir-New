import sys
import types
from pathlib import Path

from app.services.ai_host import AIHostService
from app.services.ai_host_fast import AIHostFastService


def test_fast_service_forwards_dedupe_key(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_generate(self, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(AIHostService, "generate_track_intro_announcement", _fake_generate)

    service = AIHostFastService(preload_models=False)
    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={},
        dedupe_key="ai-track-intro:15",
    )

    assert result == {"ok": True}
    assert captured["dedupe_key"] == "ai-track-intro:15"


def test_fast_service_preload_keeps_external_providers_lazy(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(AIHostFastService, "_edge_tts_available", staticmethod(lambda: True))

    def _fake_load_llm(self, model_token=""):
        calls.append("llm")
        self._llm_loaded = True
        return True

    def _fake_load_tts(self, tts_model_path=""):
        calls.append("tts")
        return True

    monkeypatch.setattr(AIHostFastService, "_load_llm", _fake_load_llm)
    monkeypatch.setattr(AIHostFastService, "_load_tts", _fake_load_tts)

    service = AIHostFastService(preload_models=False)
    status = service.preload_for_playout()

    assert calls == []
    assert status["llm_loaded"] is True
    assert status["tts_provider"] == "local-qwen-tts"


def test_fast_service_does_not_fallback_to_local_tts(monkeypatch, tmp_path):
    calls: list[str] = []

    def _fake_edge(self, text, output_path, *, persona):
        calls.append("edge")
        return False

    def _fake_local(self, text, output_path, *, persona, tts_model_path=""):
        calls.append("local")
        return True

    monkeypatch.setattr(AIHostFastService, "_synthesize_with_edge_tts", _fake_edge)
    monkeypatch.setattr(AIHostFastService, "_synthesize_with_local_tts", _fake_local)

    service = AIHostFastService(preload_models=False)
    provider = service._synthesize(
        "hello",
        tmp_path / "out.wav",
        persona="afternoon",
        settings={"ai_tts_provider": "edge-tts"},
    )

    assert provider == ""
    assert calls == ["edge"]


def test_fast_service_can_use_windows_sapi_provider(monkeypatch, tmp_path):
    calls: list[str] = []

    def _fake_windows_sapi(self, text, output_path, *, persona):
        calls.append("windows-sapi")
        Path(output_path).write_bytes(b"R" * 4096)
        return True

    monkeypatch.setattr(
        AIHostFastService,
        "_synthesize_with_windows_sapi",
        _fake_windows_sapi,
    )

    service = AIHostFastService(preload_models=False)
    provider = service._synthesize(
        "hello",
        tmp_path / "out.wav",
        persona="afternoon",
        settings={"ai_tts_provider": "windows-sapi"},
    )

    assert provider == "windows-sapi"
    assert calls == ["windows-sapi"]


def test_fast_service_can_use_omnivoice_provider(monkeypatch, tmp_path):
    calls: list[str] = []

    def _fake_omnivoice(self, text, output_path, *, persona, settings=None):
        calls.append("omnivoice")
        Path(output_path).write_bytes(b"R" * 4096)
        return True

    def _unexpected_edge(self, text, output_path, *, persona):
        raise AssertionError("edge-tts should not run when omnivoice succeeds")

    monkeypatch.setattr(AIHostFastService, "_synthesize_with_omnivoice", _fake_omnivoice)
    monkeypatch.setattr(AIHostFastService, "_synthesize_with_edge_tts", _unexpected_edge)

    service = AIHostFastService(preload_models=False)
    provider = service._synthesize(
        "hello",
        tmp_path / "out.wav",
        persona="afternoon",
        settings={"ai_tts_provider": "omnivoice"},
    )

    assert provider == "omnivoice"
    assert calls == ["omnivoice"]


def test_fast_service_does_not_fallback_to_edge_when_omnivoice_fails(monkeypatch, tmp_path):
    calls: list[str] = []

    def _fake_omnivoice(self, text, output_path, *, persona, settings=None):
        calls.append("omnivoice")
        return False

    def _unexpected_edge(self, text, output_path, *, persona):
        raise AssertionError("edge-tts should not run when omnivoice is configured")

    monkeypatch.setattr(AIHostFastService, "_synthesize_with_omnivoice", _fake_omnivoice)
    monkeypatch.setattr(AIHostFastService, "_synthesize_with_edge_tts", _unexpected_edge)

    service = AIHostFastService(preload_models=False)
    provider = service._synthesize(
        "hello",
        tmp_path / "out.wav",
        persona="afternoon",
        settings={"ai_tts_provider": "omnivoice"},
    )

    assert provider == ""
    assert calls == ["omnivoice"]


def test_edge_tts_falls_back_to_stable_voice_when_primary_voice_fails(monkeypatch, tmp_path):
    calls: list[str] = []

    class _FakeCommunicate:
        def __init__(self, text, voice, rate="+0%"):
            self.voice = voice

        async def save(self, path):
            calls.append(self.voice)
            if self.voice == "en-US-ChristopherNeural":
                raise RuntimeError("No audio was received.")
            Path(path).write_bytes(b"R" * 4096)

    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        types.SimpleNamespace(Communicate=_FakeCommunicate),
    )
    monkeypatch.setattr("app.services.ai_host.enhance_for_tts", lambda text: text)

    service = AIHostService()
    ok = service._synthesize_with_edge_tts("hello", tmp_path / "edge.wav", persona="morning")

    assert ok is True
    assert calls == ["en-US-ChristopherNeural", "en-US-ChristopherNeural", "en-US-GuyNeural"]
