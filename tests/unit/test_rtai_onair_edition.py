from __future__ import annotations

import json
from pathlib import Path

from app import product_edition
from scripts.package_rtai_onair import build


ROOT = Path(__file__).resolve().parents[2]


def test_radiotedu_remains_the_default_edition(monkeypatch):
    monkeypatch.delenv("CLEANROOM_PRODUCT_EDITION", raising=False)
    profile = product_edition.public_product_profile()

    assert profile["edition"] == "radiotedu"
    assert profile["product_name"] == "RadioTEDU OnAir"
    assert profile["features"]["voting"] is True
    assert product_edition.api_path_enabled("/api/integrations/radiotedu") is True


def test_rtai_edition_disables_radiotedu_only_api_families(monkeypatch):
    monkeypatch.setenv("CLEANROOM_PRODUCT_EDITION", "rtai-onair")
    profile = product_edition.public_product_profile()

    assert profile["product_name"] == "rtAI OnAir"
    assert profile["features"]["local_ai"] is True
    assert profile["features"]["voting"] is False
    assert product_edition.api_path_enabled("/api/ai/settings") is True
    assert product_edition.api_path_enabled("/api/recovery/points") is True
    assert product_edition.api_path_enabled("/api/campaign/voting/round") is False
    assert product_edition.api_path_enabled("/api/integrations/radiotedu/services") is False
    assert product_edition.api_path_enabled("/api/streaming/quality-outputs") is False


def test_rtai_frontend_profile_keeps_local_ai_and_skips_external_services():
    index = (ROOT / "app/static/onair/index.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/onair/app.js").read_text(encoding="utf-8")

    assert '<meta name="onair-edition" content="radiotedu">' in index
    assert "await loadProductEditionProfile();" in script
    assert "applyProductEdition();" in script
    assert "Local AI and readiness" in script
    assert "!IS_RTAI_ONAIR && view === 'services'" in script
    assert "? Promise.resolve({ services: {}, definitions: [], status: [] })" in script


def test_rtai_package_is_branded_hashed_and_not_installed(tmp_path):
    result = build(tmp_path)
    directory = Path(result["directory"])
    archive = Path(result["archive"])
    manifest = json.loads(
        (directory / "rtai-onair-manifest.json").read_text(encoding="utf-8")
    )
    index = (directory / "app/static/onair/index.html").read_text(encoding="utf-8")
    web_manifest = json.loads(
        (directory / "app/static/manifest.json").read_text(encoding="utf-8")
    )

    assert directory.parent == tmp_path
    assert archive.is_file()
    assert '<meta name="onair-edition" content="rtai-onair">' in index
    assert web_manifest["name"] == "rtAI OnAir"
    assert manifest["features"]["voting"] is False
    assert manifest["features"]["local_ai"] is True
    assert result["archive_sha256"]
    assert (directory / "START-rtAI-onair.bat").is_file()
