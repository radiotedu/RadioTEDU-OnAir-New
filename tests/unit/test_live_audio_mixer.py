from pathlib import Path


def test_live_audio_mixer_contract_file_exists():
    module_path = (
        Path(__file__).resolve().parents[2] / "app" / "audio" / "live_audio_mixer.py"
    )

    assert module_path.exists()

    source = module_path.read_text(encoding="utf-8")
    assert "class LiveAudioMixer" in source
    assert "def mix_pcm_chunk" in source
