import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import app


def test_recovery_api_creates_lists_and_reverifies_atomic_backup(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(data_root / "cleanroom.db"))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    client = TestClient(app)

    created = client.post("/api/recovery/points", json={"tier": "daily"})
    assert created.status_code == 200
    assert created.json()["verified"] is True
    assert created.json()["file_name"].endswith(".db.dpapi")

    listed = client.get("/api/recovery/points")
    assert listed.status_code == 200
    points = listed.json()["points"]
    assert len(points) == 1
    assert points[0]["tier"] == "daily"
    assert points[0]["integrity_status"] == "ok"

    verified = client.post(f"/api/recovery/points/{points[0]['id']}/verify")
    assert verified.status_code == 200
    assert verified.json()["valid"] is True

    conn = get_connection()
    try:
        stored_path = Path(
            conn.execute(
                "SELECT file_path FROM recovery_points WHERE id=?", (points[0]["id"],)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    payload = bytearray(stored_path.read_bytes())
    payload[-1] ^= 0x01
    stored_path.write_bytes(payload)

    tampered = client.post(f"/api/recovery/points/{points[0]['id']}/verify")
    assert tampered.status_code == 200
    assert tampered.json()["valid"] is False
    refused = client.post(
        f"/api/recovery/points/{points[0]['id']}/stage-restore"
    )
    assert refused.status_code == 409


def test_recovery_verification_rejects_database_path_escape(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(data_root / "cleanroom.db"))
    client = TestClient(app)
    created = client.post("/api/recovery/points", json={"tier": "hourly"})
    assert created.status_code == 200

    outside = tmp_path / "outside.db.dpapi"
    outside.write_bytes(b"not a recovery point")
    conn = get_connection()
    try:
        point_id = int(
            conn.execute("SELECT MAX(id) FROM recovery_points").fetchone()[0]
        )
        conn.execute(
            "UPDATE recovery_points SET file_path=? WHERE id=?",
            (str(outside), point_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(f"/api/recovery/points/{point_id}/verify")
    assert response.status_code == 409
    assert str(outside) not in response.text


def test_recovery_api_stages_verified_point_for_offline_supervisor_restore(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "data"
    database_path = data_root / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(database_path))
    monkeypatch.setenv("CLEANROOM_USER_CONFIG_ROOT", str(tmp_path / "user"))
    client = TestClient(app)

    created = client.post("/api/recovery/points", json={"tier": "daily"})
    assert created.status_code == 200
    conn = get_connection()
    try:
        point_id = int(conn.execute("SELECT MAX(id) FROM recovery_points").fetchone()[0])
    finally:
        conn.close()

    staged = client.post(f"/api/recovery/points/{point_id}/stage-restore")

    assert staged.status_code == 200
    assert staged.json()["staged"] is True
    assert staged.json()["restart_required"] is True
    assert set(staged.json()) == {"id", "plan_id", "restart_required", "staged"}
    pending_path = data_root / "State" / "Recovery" / "pending.json"
    plan = json.loads(pending_path.read_text(encoding="utf-8"))
    assert plan["planId"] == staged.json()["plan_id"]
    assert Path(plan["targetDatabase"]) == database_path.resolve()
    assert Path(plan["sourceDatabase"]).is_relative_to(
        (data_root / "Recovery" / "Staging").resolve()
    )
    restored = sqlite3.connect(plan["sourceDatabase"])
    try:
        assert restored.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        restored.close()

    duplicate = client.post(f"/api/recovery/points/{point_id}/stage-restore")
    assert duplicate.status_code == 409
    assert "pending" not in duplicate.text.lower()


def test_recovery_stage_rejects_database_outside_product_data_root(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "shared-data"
    database_path = tmp_path / "outside" / "cleanroom.db"
    monkeypatch.setenv("CLEANROOM_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(database_path))
    client = TestClient(app)
    created = client.post("/api/recovery/points", json={"tier": "daily"})
    assert created.status_code == 200
    conn = get_connection()
    try:
        point_id = int(conn.execute("SELECT MAX(id) FROM recovery_points").fetchone()[0])
    finally:
        conn.close()

    staged = client.post(f"/api/recovery/points/{point_id}/stage-restore")

    assert staged.status_code == 409
    assert not (data_root / "State" / "Recovery" / "pending.json").exists()
    check = sqlite3.connect(database_path)
    try:
        assert check.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    finally:
        check.close()
