import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_internal"))
sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.api.stations import StationOutputUpdate  # noqa: E402
from app.auth.password import verify_password  # noqa: E402
from app.db import _ensure_default_admin  # noqa: E402
from app.engine.runtime_registry import StationRuntimeRegistry  # noqa: E402


class SecurityDefaultTests(unittest.TestCase):
    def test_jwt_secret_is_persistent_per_install_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            with patch.dict(
                os.environ,
                {"JWT_SECRET_KEY": "", "CLEANROOM_JWT_SECRET_FILE": ""},
            ):
                with patch.object(app_config, "get_user_config_root", return_value=data_root):
                    first = app_config.get_jwt_secret_key()
                    second = app_config.get_jwt_secret_key()

            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertNotEqual(first, "cleanroom-dev-secret-change-me")
            self.assertEqual(
                (data_root / "secrets" / "jwt-signing.key").read_text(encoding="utf-8").strip(),
                first,
            )

    def test_default_admin_uses_random_initial_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(":memory:")
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username TEXT, display_name TEXT, password_hash TEXT, role TEXT, is_active INTEGER"
                ")"
            )
            with patch.dict(os.environ, {"CLEANROOM_INITIAL_ADMIN_PASSWORD": ""}):
                with patch("app.db.get_data_root", return_value=Path(tmp)):
                    _ensure_default_admin(cur)

            row = cur.execute(
                "SELECT username, password_hash FROM users WHERE username='admin'"
            ).fetchone()
            initial_file = Path(tmp) / "initial-admin-password.txt"
            initial_text = initial_file.read_text(encoding="utf-8")
            password = initial_text.split("Password: ", 1)[1].splitlines()[0]

            self.assertEqual(row[0], "admin")
            self.assertNotEqual(password, "changeme")
            self.assertTrue(verify_password(password, row[1]))

    def test_default_station_output_does_not_ship_public_icecast_password(self):
        model_defaults = StationOutputUpdate(station_id=1)
        runtime_defaults = StationRuntimeRegistry._default_output_settings(1)

        self.assertFalse(model_defaults.icecast_enabled)
        self.assertEqual(model_defaults.icecast_password, "")
        self.assertFalse(runtime_defaults["icecast_enabled"])
        self.assertEqual(runtime_defaults["icecast_password"], "")


if __name__ == "__main__":
    unittest.main()
