from pathlib import Path


def test_unified_operator_ui_exposes_all_supported_sections_without_retired_shell():
    root = Path(__file__).resolve().parents[2] / "app" / "static"
    html = (root / "onair" / "index.html").read_text(encoding="utf-8")
    sections = (
        "onair", "stations", "media", "queue", "scheduler", "dayparting",
        "automation", "emergency", "services", "settings", "diagnostics", "recovery",
    )
    for section in sections:
        assert f'data-operator-nav="{section}"' in html
        assert f'data-operator-view="{section}"' in html

    assert 'id="operatorNavigation"' in html
    assert 'id="autoPlaylistModal"' not in html
    assert "Create Auto Playlist" not in html
