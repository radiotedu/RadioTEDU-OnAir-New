import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_internal"))
sys.path.insert(0, str(ROOT))

from app.api import health as health_api  # noqa: E402


class HealthEndpointTests(unittest.TestCase):
    def test_health_uses_cached_dependency_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            db_path = data_root / "cleanroom.db"
            runtime_status = {
                "running": True,
                "branch_health": {"icecast": True, "local": False},
            }
            worker_status = {"running": True}

            with patch("app.db.get_db_path", return_value=db_path), patch(
                "app.db.get_data_root", return_value=data_root
            ), patch.object(
                health_api, "get_db_path", return_value=db_path
            ), patch.object(
                health_api.runtime_registry, "status", return_value=runtime_status
            ), patch.object(
                health_api.runtime_registry, "snapshot", return_value={}
            ), patch.object(
                health_api.worker_loop_manager, "status", return_value=worker_status
            ), patch.object(
                health_api.worker_loop_manager, "snapshot", return_value={}
            ), patch.object(
                health_api,
                "database_health_snapshot",
                return_value={
                    "healthy": True,
                    "state": "operational",
                    "integrity": "ok",
                },
            ), patch.object(
                health_api, "describe_dependency", return_value={"found": True}
            ), patch.object(
                health_api, "read_bootstrap_state", return_value={}
            ), patch(
                "app.dependency_bootstrap.refresh_dependency_state",
                side_effect=AssertionError("health must not refresh dependencies"),
            ):
                payload = health_api.health(station_id=1)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["engine_running"])
            self.assertEqual(payload["setup_dependencies"], {
                "webview2": {},
                "ollama": {},
                "python_runtime": {},
                "qwen_tts_runtime": {},
            })


if __name__ == "__main__":
    unittest.main()
