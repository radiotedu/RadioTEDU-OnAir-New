import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db as app_db
from app.api import health as health_api
from app.reliability import atomic_write_json, read_json_object


def test_atomic_json_preserves_last_valid_generation(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"generation": 1})
    atomic_write_json(target, {"generation": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 2}
    assert json.loads(
        target.with_suffix(".json.bak").read_text(encoding="utf-8")
    ) == {"generation": 1}


def test_json_read_recovers_from_corrupt_primary(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"generation": 1})
    atomic_write_json(target, {"generation": 2})
    target.write_text("{power-loss", encoding="utf-8")

    assert read_json_object(target) == {"generation": 1}


def test_concurrent_json_writes_never_leave_partial_state(tmp_path: Path):
    target = tmp_path / "state.json"
    errors = []

    def writer(generation: int):
        try:
            for sequence in range(10):
                atomic_write_json(
                    target,
                    {"generation": generation, "sequence": sequence},
                )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    value = json.loads(target.read_text(encoding="utf-8"))
    assert 0 <= int(value["generation"]) < 8
    assert 0 <= int(value["sequence"]) < 10
    assert list(tmp_path.glob("*.tmp")) == []


def test_database_connection_enforces_durable_pragmas(tmp_path: Path):
    db_path = tmp_path / "radio.db"
    with patch.object(app_db, "get_db_path", return_value=db_path):
        connection = app_db.get_connection()
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] >= 2
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            connection.close()


def test_database_health_detects_valid_database(tmp_path: Path):
    db_path = tmp_path / "radio.db"
    with patch.object(app_db, "get_db_path", return_value=db_path):
        connection = app_db.get_connection()
        connection.execute("CREATE TABLE durable_test (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()

        snapshot = app_db.database_health_snapshot(force=True)

    assert snapshot["integrity"] == "ok"
    assert snapshot["journal_mode"] == "wal"
    assert snapshot["synchronous"] in {"full", "extra"}
    assert snapshot["foreign_keys"] is True


def test_database_health_warns_but_remains_ready_with_gibibytes_free(tmp_path: Path):
    db_path = tmp_path / "radio.db"
    total = 1024 * 1024 * 1024 * 1024
    free = int(3.5 * 1024 * 1024 * 1024)
    with (
        patch.object(app_db, "get_db_path", return_value=db_path),
        patch.object(
            app_db.shutil,
            "disk_usage",
            return_value=SimpleNamespace(total=total, used=total - free, free=free),
        ),
    ):
        connection = app_db.get_connection()
        connection.execute("CREATE TABLE durable_test (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()

        snapshot = app_db.database_health_snapshot(force=True)

    assert snapshot["healthy"] is True
    assert snapshot["state"] == "degraded"
    assert snapshot["disk_free_percent"] < 3.0


def test_liveness_does_not_depend_on_database():
    payload = health_api.liveness()

    assert payload["status"] == "ok"
    assert payload["state"] == "operational"
    assert payload["service"] == "radiotedu-onair"
    assert payload["backend_instance_id"]
    assert int(payload["backend_process_id"]) > 0


def test_readiness_returns_503_for_failed_integrity():
    with patch.object(health_api, "init_db", return_value=None), patch.object(
        health_api,
        "database_health_snapshot",
        return_value={
            "state": "critical",
            "healthy": False,
            "integrity": "corrupt",
        },
    ):
        response = health_api.readiness()

    assert response.status_code == 503
    assert b'"ready":false' in response.body
