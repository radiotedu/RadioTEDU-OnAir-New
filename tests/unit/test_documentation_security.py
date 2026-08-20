from pathlib import Path


def test_reliability_runbook_never_embeds_source_password() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "BROADCAST_RELIABILITY_RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    source_password_line = next(
        line for line in text.splitlines() if "Source Password:" in line
    )

    assert "credential vault" in source_password_line
    assert "`" not in source_password_line
