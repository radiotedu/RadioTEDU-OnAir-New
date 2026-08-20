import json
from pathlib import Path

from app.services.ai_host import AIHostService


def _write_fake_audio(output_path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"R" * 4096)


def test_track_intro_fails_when_tts_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)
    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_edge_tts",
        lambda self, text, output_path, *, persona: False,
    )
    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_local_tts",
        lambda self, text, output_path, *, persona, tts_model_path="": False,
    )

    service = AIHostService()
    settings = {
        "ai_announcement_max_seconds": "18",
        "ai_include_music_history": "false",
        "ai_educational_segments": "true",
        "ai_prompt_template": "You're listening to {station_name}. Up next is {track_title}{artist_phrase}.{educational_line}",
    }

    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings=settings,
    )

    assert result is None


def test_track_intro_succeeds_with_local_tts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)
    monkeypatch.setattr(AIHostService, "_generate_text", lambda self, prompt, max_tokens=96, model_token="": "")
    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_edge_tts",
        lambda self, text, output_path, *, persona: False,
    )

    def _fake_local_tts(self, text, output_path, *, persona, tts_model_path=""):
        _write_fake_audio(output_path)
        return True

    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_local_tts",
        _fake_local_tts,
    )

    service = AIHostService()
    settings = {
        "ai_announcement_max_seconds": "18",
        "ai_include_music_history": "false",
        "ai_educational_segments": "true",
        "ai_prompt_template": "You're listening to {station_name}. Up next is {track_title}{artist_phrase}.{educational_line}",
    }

    first = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings=settings,
    )

    assert first is not None
    assert Path(first.audio_path).exists()
    assert first.llm_provider == "template-fallback"
    assert first.tts_provider == "local-qwen-tts"
    assert "Radio TEDU" in first.text
    assert "Clair de Lune" in first.text


def test_track_intro_prefers_edge_tts_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)

    def _fake_edge_tts(self, text, output_path, *, persona):
        _write_fake_audio(output_path)
        return True

    def _unexpected_local_tts(self, text, output_path, *, persona, tts_model_path=""):
        raise AssertionError("local TTS should not run when edge-tts succeeds")

    monkeypatch.setattr(AIHostService, "_synthesize_with_edge_tts", _fake_edge_tts)
    monkeypatch.setattr(AIHostService, "_synthesize_with_local_tts", _unexpected_local_tts)

    service = AIHostService()
    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={
            "ai_tts_provider": "edge-tts",
            "ai_prompt_template": "Up next is {track_title}{artist_phrase}.",
        },
    )

    assert result is not None
    assert result.tts_provider == "edge-tts"
    assert Path(result.audio_path).exists()


def test_track_intro_can_use_windows_sapi_provider(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)

    def _fake_windows_sapi(self, text, output_path, *, persona):
        _write_fake_audio(output_path)
        return True

    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_windows_sapi",
        _fake_windows_sapi,
    )

    service = AIHostService()
    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={
            "ai_tts_provider": "windows-sapi",
            "ai_prompt_template": "Up next is {track_title}{artist_phrase}.",
        },
    )

    assert result is not None
    assert result.tts_provider == "windows-sapi"
    assert Path(result.audio_path).exists()


def test_windows_sapi_status_is_operational_when_voice_is_available(monkeypatch):
    monkeypatch.setattr(AIHostService, "_windows_sapi_available", lambda self: True)

    service = AIHostService()
    status = service.get_status(settings={"ai_tts_provider": "windows-sapi"}, station_id=1)

    assert status["ready"] is True
    assert status["tts_loaded"] is True
    assert status["tts_model_exists"] is True
    assert status["tts_provider"] == "windows-sapi"


def test_track_intro_can_use_omnivoice_provider(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)

    def _fake_omnivoice(self, text, output_path, *, persona, settings=None):
        _write_fake_audio(output_path)
        return True

    def _unexpected_edge(self, text, output_path, *, persona):
        raise AssertionError("edge-tts should not run when omnivoice succeeds")

    monkeypatch.setattr(AIHostService, "_synthesize_with_omnivoice", _fake_omnivoice)
    monkeypatch.setattr(AIHostService, "_synthesize_with_edge_tts", _unexpected_edge)

    service = AIHostService()
    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={
            "ai_tts_provider": "omnivoice",
            "ai_prompt_template": "Up next is {track_title}{artist_phrase}.",
            "ai_announcement_max_seconds": "15",
        },
    )

    assert result is not None
    assert result.tts_provider == "omnivoice"
    assert Path(result.audio_path).exists()


def test_track_intro_does_not_fallback_to_edge_when_omnivoice_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_omnivoice",
        lambda self, text, output_path, *, persona, settings=None: False,
    )

    def _unexpected_edge(self, text, output_path, *, persona):
        raise AssertionError("edge-tts should not run when omnivoice is configured")

    monkeypatch.setattr(AIHostService, "_synthesize_with_edge_tts", _unexpected_edge)

    service = AIHostService()
    result = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={
            "ai_tts_provider": "omnivoice",
            "ai_prompt_template": "Up next is {track_title}{artist_phrase}.",
            "ai_announcement_max_seconds": "15",
        },
    )

    assert result is None


def test_unknown_llm_token_does_not_alias_to_default_local_model(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    default_llm_dir = models_dir / "qwen2.5-0.5b-instruct"
    default_llm_dir.mkdir()

    monkeypatch.setattr("app.services.ai_host.MODELS_DIR", models_dir)
    monkeypatch.setattr("app.services.ai_host.DEFAULT_LLM_LOCAL_DIR", default_llm_dir)

    service = AIHostService()

    assert service._resolve_llm_dir("") == default_llm_dir
    assert service._resolve_llm_dir("Qwen/Qwen2.5-0.5B-Instruct") == default_llm_dir
    assert service._resolve_llm_dir("Qwen/live-check") is None
    assert service._llm_provider_status("Qwen/live-check") == "template-fallback"


def test_dedupe_key_creates_fresh_cache_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.ai_host.CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.services.ai_host.probe_duration", lambda _path: 6.4)
    monkeypatch.setattr(
        AIHostService,
        "_synthesize_with_edge_tts",
        lambda self, text, output_path, *, persona: False,
    )

    def _fake_local_tts(self, text, output_path, *, persona, tts_model_path=""):
        _write_fake_audio(output_path)
        return True

    monkeypatch.setattr(AIHostService, "_synthesize_with_local_tts", _fake_local_tts)

    service = AIHostService()
    first = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={"ai_prompt_template": "Up next is {track_title}{artist_phrase}."},
    )
    second = service.generate_track_intro_announcement(
        station_id=1,
        station_name="Radio TEDU",
        title="Clair de Lune",
        artist="Debussy",
        settings={"ai_prompt_template": "Up next is {track_title}{artist_phrase}."},
        dedupe_key="ai-track-intro:42",
    )

    assert first is not None
    assert second is not None
    metadata_path = tmp_path / f"announcement_{second.cache_key}.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert second.cache_key != first.cache_key
    assert second.dedupe_key == "ai-track-intro:42"
    assert payload["dedupe_key"] == "ai-track-intro:42"
