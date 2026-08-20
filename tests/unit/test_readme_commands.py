from pathlib import Path


def test_readme_contains_run_commands():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert "uvicorn app.main:app" in text
    assert "/api/auth/login" in text
    assert "python -m pytest" in text
    assert "import_legacy_data.py" in text
    assert "music -> music" in text
    assert "hard cut" in text


def test_readme_documents_phase_4a_deploy_path():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert "Local development stays simple: `uvicorn app.main:app --reload` or `python run_cleanroom.py`." in text
    assert "For reverse-proxy deployment, place the app behind HTTPS and let the browser reach `/ws` over WSS." in text
    assert "The recommended proxy path is Caddy or an equivalent HTTPS terminator. Keep the app itself on HTTP and terminate TLS at the proxy." in text
    assert "Set `PUBLIC_BASE_URL` when you know the external origin, for example `https://radio.example.com`." in text
    assert "Set `CORS_ORIGINS` to the public origins that should be allowed, for example `https://radio.example.com,https://ops.example.com`." in text
    assert "Set `TRUST_PROXY_HEADERS=true` when the app is behind a trusted reverse proxy that overwrites `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-For` for login rate limiting." in text
    assert "Leave `SECURITY_HEADERS_ENABLED` on unless you are explicitly debugging a browser quirk." in text
    assert "WEBRTC_ENABLED" in text
    assert "WEBRTC_TURN_URL" in text
    assert "Confirm the websocket connects to `wss://<public-host>/ws?token=...&station_id=...`." in text
    assert "Confirm `/api/*` traffic is not being cached by the service worker." in text


def test_phase_4a_design_labels_implemented_slice_notes():
    text = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "plans"
        / "2026-03-20-radio-mode-phase-4a-hardening-design.md"
    ).read_text(encoding="utf-8")
    assert "Phase 4A is intentionally implemented as a deploy-hardening slice, not a transport rewrite." in text
    assert "- `PUBLIC_BASE_URL`, `CORS_ORIGINS`, `TRUST_PROXY_HEADERS`, and `SECURITY_HEADERS_ENABLED` are the deployment-facing knobs used by the app." in text
    assert "- The browser still uses the existing authenticated WebSocket path for live updates and mic control." in text
    assert "- The current remote mic path remains WebSocket plus `MediaRecorder`; WebRTC and TURN/STUN are still deferred." in text
    assert "- The PWA shell is intentionally conservative: shell assets can be cached, but authenticated API responses are not cached." in text
    assert "- The mobile polish is intentionally bounded to ergonomics on the existing operator shell, not a layout redesign." in text
    assert "### 3a. Deferred Future-Hardening Items" in text
    assert "- `SESSION_COOKIE_SECURE` or an equivalent secure-cookie flag" in text
    assert "- `Permissions-Policy` for a limited browser surface" in text
