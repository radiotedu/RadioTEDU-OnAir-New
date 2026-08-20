from pathlib import Path


def test_root_docs_describe_radiotedu_as_the_product():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()
    assert "radiotedu onair" in lowered
    assert "packaged backend bundle" in lowered
    assert "build_backend_onefile.ps1" in text
    assert "package_portable_release.ps1" in text
    assert "import_legacy_data.py" in text
    assert "first launch" in lowered
    assert "ready-to-stream" in lowered
    assert "yt-dlp" in lowered
    assert "ffmpeg" in lowered
    assert "ffprobe" in lowered


def test_legacy_product_entrypoints_are_gone():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "main.js").exists()
    assert not (root / "package.json").exists()
    assert not (root / "package-lock.json").exists()
    assert not (root / "start_radio.bat").exists()
    assert not (root / "backend" / "main.py").exists()
    assert not (root / "backend" / "database.py").exists()
    assert not (root / "backend" / "liquidsoap_controller.py").exists()
    assert not (root / "frontend" / "index.html").exists()
    assert not (root / "frontend" / "static" / "js" / "app.js").exists()
    assert not (root / "liquidsoap" / "main.liq").exists()


def test_public_branding_docs_do_not_present_cleanroom_as_product_name():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "cleanroom radio" not in text
    assert "radiotedu onair" in text


def test_required_operator_documents_are_present_and_linked():
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    required = (
        "DETERMINISTIC_OPERATOR_GUIDE.md",
        "TROUBLESHOOTING.md",
        "CONFIGURATION_REFERENCE.md",
        "TEST_REPORT.md",
    )

    for name in required:
        assert (root / "docs" / name).is_file()
        assert f"docs/{name}" in readme
