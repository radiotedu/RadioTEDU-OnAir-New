from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_manifest_file_exists():
    assert (ROOT / "app" / "static" / "manifest.json").exists()


def test_service_worker_file_exists():
    assert (ROOT / "app" / "static" / "sw.js").exists()


def test_html_shells_include_pwa_metadata():
    operator_html = (ROOT / "app" / "static" / "onair" / "index.html").read_text(encoding="utf-8")
    manifest = (ROOT / "app" / "static" / "manifest.json").read_text(encoding="utf-8")
    assert '"src": "/static/icons/icon-192.png"' in manifest
    assert '"sizes": "192x192"' in manifest
    assert '"src": "/static/icons/icon-512.png"' in manifest
    assert '"sizes": "512x512"' in manifest

    assert 'rel="manifest"' in operator_html
    assert "theme-color" in operator_html
    assert "viewport-fit=cover" in operator_html
    assert "navigator.serviceWorker.register('/sw.js', { scope: '/' })" in operator_html


def test_unified_sign_in_shell_does_not_disclose_credential_storage():
    operator_html = (ROOT / "app" / "static" / "onair" / "index.html").read_text(encoding="utf-8")

    assert 'id="loginForm"' in operator_html
    assert "initial-admin-password.txt" not in operator_html
    assert "%ProgramData%\\RadioTEDU\\OnAir" not in operator_html
