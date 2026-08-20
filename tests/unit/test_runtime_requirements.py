from pathlib import Path


def test_runtime_requirements_include_websocket_support_and_compatible_bcrypt():
    requirements = (
        Path(__file__).resolve().parents[2] / "requirements.txt"
    ).read_text(encoding="utf-8")

    assert "websockets" in requirements
    assert "bcrypt<4.1" in requirements
    assert "python-multipart" in requirements
    assert "requests" in requirements
