from pathlib import Path


def test_readme_documents_foundation_verification():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert "python -m pytest tests -q" in text
    assert "JWT-protected APIs" in text
    assert "/login.html" in text
    assert "queue API now persists to SQLite" in text
    assert "push-to-talk" in text.lower()
    assert "always-on" in text.lower()
