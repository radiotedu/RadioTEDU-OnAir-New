from io import BytesIO
import json
import zipfile

from fastapi.testclient import TestClient

from app.main import app


def test_radio_diagnostic_bundle_is_bounded_redacted_and_downloadable(
    tmp_path,
    monkeypatch,
):
    data_root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(data_root / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    secret = "diagnostic-canary-secret"
    monkeypatch.setattr(
        "app.services.diagnostic_bundle._known_secrets",
        lambda: (secret,),
    )
    log_root = data_root / "Logs" / "Supervisor"
    log_root.mkdir(parents=True)
    (log_root / "supervisor.log").write_text(
        f"password={secret}\nAuthorization: Bearer {secret}\n",
        encoding="utf-8",
    )
    crash_root = data_root / "CrashDumps"
    crash_root.mkdir(parents=True)
    (crash_root / "RadioTEDU-OnAir-Backend.1234.dmp").write_bytes(
        b"binary-memory-" + secret.encode("utf-8")
    )

    client = TestClient(app)
    created = client.post("/api/recovery/diagnostics")
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["name"].startswith("radiotedu-diagnostics-")
    assert len(result["sha256"]) == 64

    listed = client.get("/api/recovery/diagnostics")
    assert listed.status_code == 200
    assert listed.json()["bundles"][0]["name"] == result["name"]

    downloaded = client.get(result["download_url"])
    assert downloaded.status_code == 200
    with zipfile.ZipFile(BytesIO(downloaded.content), "r") as archive:
        names = archive.namelist()
        extracted = b"\n".join(archive.read(name) for name in names)
        inventory = json.loads(archive.read("crash-inventory.json"))
    assert "manifest.json" in names
    assert "database-evidence.json" in names
    assert not any(name.endswith(".dmp") for name in names)
    assert inventory[0]["name"].endswith(".dmp")
    assert secret.encode("utf-8") not in extracted
    assert b"<redacted>" in extracted


def test_radio_diagnostic_download_rejects_path_escape(client):
    response = client.get("/api/recovery/diagnostics/not-a-bundle.zip")
    assert response.status_code == 404
