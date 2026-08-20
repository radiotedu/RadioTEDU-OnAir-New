from pathlib import Path


def test_idle_timeout_banner_hidden_attribute_is_not_overridden_by_css():
    css = (Path(__file__).resolve().parents[2] / "app" / "static" / "onair" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert ".idle-timeout-banner[hidden]" in css
    assert "display: none !important;" in css
