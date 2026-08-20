from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.quality_output_bridge import (
    build_quality_bridge_payload,
    inspect_quality_bridge,
    write_quality_bridge,
)
def _settings() -> dict[str, str]:
    return {}


class QualityOutputBridgeTests(unittest.TestCase):
    def test_payload_marks_ai_as_legacy_only_and_contains_no_credentials(self):
        payload = build_quality_bridge_payload(_settings())
        encoded = json.dumps(payload)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["channels"]), 0)
        self.assertFalse(payload["credentials_included"])
        self.assertNotIn("password", encoded.lower())
        self.assertNotIn("secret", encoded.lower())

    def test_bridge_write_is_atomic_read_back_verified_and_backed_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "shared" / "quality-outputs.json"
            backups = root / "backup"
            environment = {
                "CLEANROOM_QUALITY_OUTPUT_BRIDGE_PATH": str(target),
                "CLEANROOM_QUALITY_OUTPUT_BRIDGE_BACKUP_ROOT": str(backups),
            }
            with patch.dict(os.environ, environment, clear=False):
                result = write_quality_bridge(_settings())
                inspection = inspect_quality_bridge()

            self.assertTrue(result["ok"])
            self.assertTrue(result["backup_verified"])
            self.assertEqual(result["channel_count"], 0)
            self.assertEqual(result["mount_count"], 0)
            self.assertTrue(target.is_file())
            self.assertEqual(len(list(backups.glob("quality-outputs-*.json"))), 1)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
            self.assertEqual(
                target.read_bytes(),
                next(backups.glob("quality-outputs-*.json")).read_bytes(),
            )
            self.assertTrue(inspection["ok"])
            self.assertEqual(inspection["mount_count"], 0)
            self.assertFalse(inspection["credentials_included"])

    def test_bridge_inspection_rejects_a_tampered_secret_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "quality-outputs.json"
            payload = build_quality_bridge_payload(_settings())
            payload["password"] = "must-not-pass"
            target.write_text(json.dumps(payload), encoding="utf-8")

            with patch.dict(
                os.environ,
                {"CLEANROOM_QUALITY_OUTPUT_BRIDGE_PATH": str(target)},
                clear=False,
            ):
                inspection = inspect_quality_bridge()

            self.assertFalse(inspection["ok"])
            self.assertTrue(inspection["credentials_included"])


if __name__ == "__main__":
    unittest.main()
