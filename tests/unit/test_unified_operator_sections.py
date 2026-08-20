import re
from pathlib import Path


def test_unified_operator_surface_exposes_all_required_sections():
    root = Path(__file__).resolve().parents[2]
    html = (root / "app" / "static" / "onair" / "index.html").read_text(
        encoding="utf-8"
    )
    javascript = (root / "app" / "static" / "onair" / "app.js").read_text(
        encoding="utf-8"
    )
    required = {
        "onair",
        "stations",
        "media",
        "playlists",
        "queue",
        "scheduler",
        "dayparting",
        "automation",
        "emergency",
        "services",
        "diagnostics",
        "settings",
        "recovery",
        "shows",
        "compliance",
        "ads",
        "streaming",
    }

    navigation = set(re.findall(r'data-operator-nav="([a-z]+)"', html))
    fragments = set(re.findall(r'data-operator-view="([a-z]+)"', html))
    definitions = set(
        re.findall(r"^\s{2}([a-z]+): \{ eyebrow:", javascript, re.MULTILINE)
    )
    assert navigation == required
    assert required <= fragments
    assert definitions == required
    assert 'id="scheduleForm"' in html
    assert 'id="recoveryForm"' in html
    assert 'id="showForm"' in html
    assert 'id="musicUsageFilterForm"' in html
    assert 'id="streamingFeaturesForm"' in html
    assert 'id="adBreakSetForm"' in html
    assert 'id="playlistCreateForm"' in html
    assert 'id="autoplayShuffleSeed"' in html
    assert 'id="jukeLibraryUploadForm"' in html
    assert 'id="jukeLibrarySearchForm"' in html
    assert "/api/schedule/items" in javascript
    assert "/api/recovery/points" in javascript
    assert "/api/shows/" in javascript
    assert "/api/music-usage" in javascript
    assert "/api/streaming/health" in javascript
    assert "/api/ad-campaigns" in javascript
    assert "/api/playlists/auto/generate" in javascript
    assert "playback_selection_policy: 'stable_rotation'" in javascript
    assert "/api/integrations/radiotedu/juke-library/upload" in javascript
    assert "/api/integrations/radiotedu/juke-library/${action}" in javascript
    assert "confirmation: 'RETIRE JUKE SONG'" in javascript
    assert "confirmation: 'RESTORE JUKE SONG'" in javascript
