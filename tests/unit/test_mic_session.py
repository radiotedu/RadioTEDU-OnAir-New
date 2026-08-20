from pathlib import Path


def test_mic_session_contract_file_exists():
    module_path = (
        Path(__file__).resolve().parents[2] / "app" / "audio" / "mic_session.py"
    )

    assert module_path.exists()

    source = module_path.read_text(encoding="utf-8")
    assert "class MicSession" in source
    assert "def start" in source
    assert "def push_chunk" in source
    assert "def read_pcm" in source
    assert "def stop" in source
    assert "def snapshot" in source
