from __future__ import annotations

import unittest

from app.audio.gst_pipeline import StationPipelineConfig
from app.audio.metadata_policy import icecast_metadata_outputs


class QualityMetadataSuppressionTests(unittest.TestCase):
    def test_admin_now_playing_is_not_sent_to_suppressed_quality_mounts(self):
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="protected-secret",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Internal title",
            stream_artist="Internal artist",
            extra_icecast_outputs=(
                {
                    "enabled": True,
                    "icecast_mount": "/lofi-low",
                    "metadata_suppressed": True,
                },
                {
                    "enabled": True,
                    "icecast_mount": "/generic-metadata-output",
                    "metadata_suppressed": False,
                },
            ),
        )
        mounts = [item["icecast_mount"] for item in icecast_metadata_outputs(cfg)]

        self.assertEqual(mounts, ["/lofi", "/generic-metadata-output"])
        self.assertNotIn("/lofi-low", mounts)


if __name__ == "__main__":
    unittest.main()
