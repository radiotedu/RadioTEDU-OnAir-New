import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import verify_broadcast_pc_commissioning as verifier


class PassingProbes:
    def listeners(self):
        return {3210: {"127.0.0.1"}, 4317: {"127.0.0.1"}, 4320: {"127.0.0.1"}}

    def json(self, url, _timeout):
        if "juke" in url:
            return 200, {
                "foreground_passed": True, "mirror_enabled": False, "autoplay_enabled": False,
                "wss_connected": True, "heartbeat_status": 204, "reconnect_passed": True,
            }
        if "voting" in url:
            return 200, {
                "foreground_passed": True, "wss_authenticated": True, "reconnect_passed": True,
                "icecast_connected": True, "ai_source_owner": "voting",
            }
        return 200, {"status": "ok"}

    def status(self, _url, _timeout):
        return 200

    def audio(self, _url, _timeout):
        return 200, {"Content-Type": "audio/mpeg"}, 512

    def decode_public_audio(self, _url, seconds, _timeout):
        return seconds == 30


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _config(tmp_path: Path) -> Path:
    music = tmp_path / "music"
    music.mkdir()
    ai_env = tmp_path / "ai.env"
    ai_env.write_text("PUBLIC_STATE_ONLY=true\n", encoding="utf-8")
    public_state = tmp_path / "public-state.json"
    _write_json(public_state, {"api_origin": "https://radiotedu.com"})
    fingerprint = tmp_path / "fingerprint.txt"
    fingerprint.write_text("version=1\n", encoding="utf-8")
    config = tmp_path / "verifier.json"
    _write_json(config, {
        "music_library_path": str(music),
        "juke_health_url": "http://127.0.0.1:3210/juke-health",
        "voting_health_url": "http://127.0.0.1:4317/voting-health",
        "voting_audio_url": "http://127.0.0.1:4320/ai",
        "public_ai_url": "https://stream.example.test/ai",
        "event_url": "https://radiotedu.com/event",
        "en_status_url": "https://radiotedu.com/status/en",
        "fr_status_url": "https://radiotedu.com/status/fr",
        "ai_env_file": str(ai_env),
        "public_state_config_file": str(public_state),
        "fingerprint_files": [str(fingerprint), str(ai_env), str(public_state)],
        "evidence_file": str(tmp_path / "evidence" / "preflight-evidence.json"),
        "request_timeout_seconds": 1,
        "decoder_path": "ffmpeg",
    })
    return config


def test_passing_cycle_writes_installer_compatible_secret_free_evidence(tmp_path):
    config = verifier.load_config(_config(tmp_path))
    evidence = verifier.verify(config, PassingProbes())
    raw = config.evidence_file.read_text(encoding="utf-8")
    assert evidence["schemaVersion"] == 1
    assert all(evidence["checks"].values())
    assert evidence["checks"]["votingSoleAiSource"] is True
    for required in (
        "publicAiDecode30Seconds", "publicEventEndpointChecked", "radioTeduEnEndpoint200",
        "radioTeduFrEndpoint200", "aiPublicStateMountless", "aiPublicStateSourceFingerprintVerified",
    ):
        assert evidence["checks"][required] is True
    assert "password" not in raw.lower()
    assert "secret" not in raw.lower()


def test_any_failure_blocks_evidence_write(tmp_path):
    config = verifier.load_config(_config(tmp_path))

    class BadLoopback(PassingProbes):
        def listeners(self):
            return {3210: {"0.0.0.0"}, 4317: {"127.0.0.1"}, 4320: {"127.0.0.1"}}

    with pytest.raises(verifier.CommissioningError, match="jukeLoopback3210"):
        verifier.verify(config, BadLoopback())
    assert not config.evidence_file.exists()


def test_mountless_check_rejects_audio_source_settings(tmp_path):
    config_path = _config(tmp_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    Path(data["ai_env_file"]).write_text("PUBLIC_STATE_ONLY=true\nAI_ICECAST_ENABLED=true\n", encoding="utf-8")
    config = verifier.load_config(config_path)
    with pytest.raises(verifier.CommissioningError, match="mountlessAiPublicStateConfig"):
        verifier.verify(config, PassingProbes())


def test_config_rejects_credentials_and_nonloopback_local_urls(tmp_path):
    config_path = _config(tmp_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["api_token"] = "not-accepted"
    _write_json(config_path, data)
    with pytest.raises(verifier.CommissioningError):
        verifier.load_config(config_path)
    data.pop("api_token")
    data["juke_health_url"] = "http://0.0.0.0:3210/health"
    _write_json(config_path, data)
    with pytest.raises(verifier.CommissioningError):
        verifier.load_config(config_path)


def test_public_decode_probe_uses_thirty_second_requirement(tmp_path):
    config = verifier.load_config(_config(tmp_path))

    class ShortDecode(PassingProbes):
        def decode_public_audio(self, _url, seconds, _timeout):
            assert seconds == 30
            return False

    with pytest.raises(verifier.CommissioningError, match="publicAiContinuousDecodable30s"):
        verifier.verify(config, ShortDecode())


def test_source_or_config_change_during_probe_blocks_evidence_write(tmp_path):
    config = verifier.load_config(_config(tmp_path))

    class MutatingProbes(PassingProbes):
        def listeners(self):
            config.ai_env_file.write_text("PUBLIC_STATE_ONLY=true\n# changed during verification\n", encoding="utf-8")
            return super().listeners()

    with pytest.raises(verifier.CommissioningError, match="sourceConfigFingerprintsStable"):
        verifier.verify(config, MutatingProbes())
    assert not config.evidence_file.exists()


def test_missing_fingerprinted_file_during_probe_is_never_attested(tmp_path):
    config = verifier.load_config(_config(tmp_path))
    deleted = config.fingerprint_files[0]

    class DeletingProbes(PassingProbes):
        def listeners(self):
            deleted.unlink()
            return super().listeners()

    with pytest.raises(verifier.CommissioningError, match="sourceConfigFingerprintsStable"):
        verifier.verify(config, DeletingProbes())
    assert not config.evidence_file.exists()


def test_system_decoder_rejects_a_short_successful_decode(monkeypatch):
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(verifier.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    assert verifier.SystemProbes("ffmpeg").decode_public_audio("https://stream.example.test/ai", 30, 45) is False
