from fastapi.testclient import TestClient

from app.main import app


def test_unhandled_api_exception_returns_json_payload():
    path = "/api/__test__/boom"

    @app.get(path)
    def _boom():
        raise RuntimeError("boom")

    try:
        client = TestClient(app, raise_server_exceptions=False)
        res = client.get(path)
        assert res.status_code == 500
        assert str(res.headers.get("content-type", "")).startswith("application/json")
        assert res.headers.get("cache-control") == "no-store"
        assert str(res.headers.get("x-request-id", "")).strip() != ""
        payload = res.json()
        assert payload.get("detail") == "internal_server_error"
        assert payload.get("message") == "Unexpected server error"
        assert str(payload.get("request_id", "")).strip() != ""
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if not (getattr(route, "path", "") == path and "GET" in (route.methods or set()))
        ]
