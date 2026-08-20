"""Visible, isolated mouse verification for the deterministic operator wall.

This is deliberately an explicit script rather than a pytest test: it opens a
real visible browser window, while the normal automated suite must stay
headless and non-interactive.  The page is served from a temporary loopback
HTTP server and every management API request is fulfilled by an inert mock.
No RadioTEDU backend, Windows service, media agent, or broadcast mount is
contacted.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Route, sync_playwright


APP_ROOT = Path(__file__).resolve().parents[2] / "app"
NAVIGATION = (
    ("onair", "onair"),
    ("media", "media"),
    ("automation", "automation"),
    ("emergency", "emergency"),
    ("services", "services"),
    ("settings", "settings"),
    ("diagnostics", "diagnostics"),
)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _static_wall_server() -> Iterator[str]:
    def handler(*args: object, **kwargs: object) -> _QuietStaticHandler:
        return _QuietStaticHandler(*args, directory=str(APP_ROOT), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/app"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _mock_api(route: Route) -> None:
    url = route.request.url.split("?", 1)[0]
    if url.endswith("/api/stations"):
        payload: object = {"stations": [{"id": 1, "name": "Isolated Test Station"}]}
    elif url.endswith("/api/stations/active"):
        payload = {"station_id": 1}
    else:
        payload = {}
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(payload),
    )


def main() -> int:
    state_changing_requests: list[str] = []
    checks: list[dict[str, object]] = []

    with _static_wall_server() as wall_url, sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False)
        except Exception:
            browser = playwright.chromium.launch(channel="chrome", headless=False)

        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(
            "localStorage.setItem('cleanroom_auth_access_token', 'isolated-test-token');"
        )
        page.route("**/api/**", _mock_api)
        page.on(
            "request",
            lambda request: state_changing_requests.append(
                f"{request.method} {request.url}"
            )
            if request.method not in {"GET", "HEAD", "OPTIONS"}
            else None,
        )
        page.goto(wall_url, wait_until="domcontentloaded")
        # Authentication/backend behavior is covered elsewhere.  This explicit
        # visual check keeps the production login and backend disconnected while
        # revealing the already-initialized operator shell for pointer testing.
        page.wait_for_timeout(500)
        page.evaluate(
            """
            document.getElementById('authGate').hidden = true;
            document.getElementById('appShell').hidden = false;
            """
        )

        for view, expected_hash in NAVIGATION:
            button = page.locator(f'[data-operator-nav="{view}"]')
            if button.count() != 1:
                raise AssertionError(f"navigation button is not unique: {view}")
            button.click()
            active_views = page.locator('[data-operator-view]:not([hidden])')
            wrong_views = page.locator(
                f'[data-operator-view]:not([hidden]):not([data-operator-view="{view}"])'
            )
            current_buttons = page.locator('[data-operator-nav][aria-current="page"]')
            checks.append(
                {
                    "view": view,
                    "visible_views": active_views.count(),
                    "wrong_visible_views": wrong_views.count(),
                    "current_buttons": current_buttons.count(),
                    "hash": page.url.rsplit("#", 1)[-1],
                }
            )
            if (
                active_views.count() < 1
                or wrong_views.count() != 0
                or current_buttons.count() != 1
            ):
                raise AssertionError(
                    "operator navigation state is ambiguous: "
                    f"{view} (visible={active_views.count()}, "
                    f"wrong={wrong_views.count()}, current={current_buttons.count()})"
                )
            if page.url.rsplit("#", 1)[-1] != expected_hash:
                raise AssertionError(f"operator navigation hash did not update: {view}")

        page.locator('[data-operator-nav="onair"]').click()
        state_changing_requests.clear()
        page.locator("#stopBroadcastButton").evaluate(
            "element => { element.disabled = false; }"
        )
        for selector in ("#startBroadcastButton", "#stopBroadcastButton"):
            control = page.locator(selector)
            if control.count() != 1:
                raise AssertionError(f"missing guarded control: {selector}")
            control.click()
        if state_changing_requests:
            raise AssertionError(
                "first-click broadcast guards issued a state-changing request: "
                + ", ".join(state_changing_requests)
            )

        page.locator('[data-operator-nav="emergency"]').click()
        state_changing_requests.clear()
        emergency = page.locator("#startEmergencyButton")
        if emergency.count() != 1:
            raise AssertionError("missing guarded emergency control")
        emergency.click()
        if state_changing_requests:
            raise AssertionError(
                "first-click emergency guard issued a state-changing request: "
                + ", ".join(state_changing_requests)
            )

        browser.close()

    print(
        json.dumps(
            {
                "result": "passed",
                "visible_browser": True,
                "isolated_api": True,
                "navigation_checks": checks,
                "guarded_controls": [
                    "start broadcast",
                    "stop broadcast",
                    "arm emergency",
                ],
                "state_changing_requests": state_changing_requests,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
