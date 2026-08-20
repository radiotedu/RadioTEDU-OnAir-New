import re
from pathlib import Path

from app.main import app


def _extract_frontend_api_paths(js_text: str) -> set[str]:
    endpoints: set[str] = set()
    pattern = r"(?P<q>['\"`])(?P<s>[^'\"`]*?/api/[^'\"`]*)(?P=q)"
    for match in re.finditer(pattern, js_text):
        token = match.group("s")
        idx = token.find("/api/")
        if idx < 0:
            continue
        endpoint = token[idx:].strip().split("?")[0]
        endpoint = re.sub(r"\$\{[^}]+\}", "{var}", endpoint)
        endpoint = endpoint.rstrip("/") if endpoint != "/" else endpoint
        if endpoint.startswith("/api/"):
            endpoints.add(endpoint)
    return endpoints


def _canonical_path(path: str) -> str:
    normalized = path.rstrip("/") if path != "/" else path
    normalized = re.sub(r"\{[^}]+\}", "{var}", normalized)
    normalized = re.sub(r"/\d+", "/{var}", normalized)
    return normalized


def test_frontend_api_paths_exist_in_backend_openapi():
    js_path = Path(__file__).resolve().parents[2] / "app" / "static" / "onair" / "app.js"
    js_text = js_path.read_text(encoding="utf-8", errors="ignore")
    frontend_paths = _extract_frontend_api_paths(js_text)
    assert len(frontend_paths) >= 40

    openapi_paths = set(app.openapi().get("paths", {}).keys())
    backend_canonical = {_canonical_path(path) for path in openapi_paths}
    missing = sorted(
        path for path in frontend_paths if _canonical_path(path) not in backend_canonical
    )
    assert missing == [], f"Frontend paths missing in backend: {missing}"
