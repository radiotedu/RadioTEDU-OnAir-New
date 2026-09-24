from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "app" / "static" / "onair" / "app.js"
GUEST_ROOM_JS = ROOT / "app" / "static" / "onair" / "guest-room.js"


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    next_async_function = source.find("\nasync function ", start + 1)
    candidates = [position for position in (next_function, next_async_function) if position >= 0]
    return source[start : min(candidates) if candidates else len(source)]


def test_unified_frontend_raw_fetch_calls_are_strictly_whitelisted():
    lines = APP_JS.read_text(encoding="utf-8", errors="ignore").splitlines()
    raw_fetch_lines = [line.strip() for line in lines if "fetch(" in line]

    assert len(raw_fetch_lines) == 3
    assert "return await fetch(url" in raw_fetch_lines[0]
    assert "/api/audio/live/render/stop" in raw_fetch_lines[1]
    assert "/api/audio/live/settings" in raw_fetch_lines[2]
    assert all("keepalive: true" in line for line in raw_fetch_lines[1:])


def test_operator_polling_lifecycle_is_bound_to_authenticated_shell():
    source = APP_JS.read_text(encoding="utf-8", errors="ignore")
    show_app = _function_source(source, "showApp")
    show_login = _function_source(source, "showLogin")

    assert "startRefreshTimer();" in show_app
    assert "startTimelineTimer();" in show_app
    assert "startIdleTimer();" in show_app
    assert "stopRefreshTimer();" in show_login
    assert "stopTimelineTimer();" in show_login
    assert "stopIdleTimer();" in show_login


def test_background_refresh_skips_hidden_or_busy_operator_shell():
    source = APP_JS.read_text(encoding="utf-8", errors="ignore")
    polling = _function_source(source, "startRefreshTimer")

    assert "!state.busy && !document.hidden" in polling
    assert "Promise.all([loadCoreStatus(), loadQueue()])" in polling
    assert "}, 5000);" in polling


def test_boot_defers_station_state_until_session_is_validated():
    source = APP_JS.read_text(encoding="utf-8", errors="ignore")
    boot = _function_source(source, "boot")

    assert boot.index("await ensureSignedIn()") < boot.index("await showApp()")
    assert "showLogin();" in boot


def test_guest_room_rest_calls_use_authenticated_operator_api_only():
    source = GUEST_ROOM_JS.read_text(encoding="utf-8", errors="ignore")

    assert "fetch(" not in source
    assert "async function request(path, options = {}) { return api(path" in source
