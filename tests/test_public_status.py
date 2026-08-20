import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_internal"))
sys.path.insert(0, str(ROOT))

from app.api import public as public_api  # noqa: E402
from app.api.public import _probe_icecast_origin, _public_status_summary  # noqa: E402


class _ProbeResponse:
    def __init__(
        self,
        *,
        status=200,
        content_type="audio/aac",
        payload=b"\xff",
    ):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size):
        return self._payload[:size]


class PublicStatusTests(unittest.TestCase):
    def setUp(self):
        public_api._origin_probe_cache.clear()

    def test_required_output_degradation_is_not_reported_live(self):
        status, reason = _public_status_summary(
            {
                "running": True,
                "branch_health": {
                    "icecast": False,
                    "local": True,
                    "icecast:/backup": False,
                },
                "required_outputs": {
                    "icecast": True,
                    "local": True,
                    "icecast:/backup": True,
                },
            },
            {"running": True, "last_error": ""},
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(reason, "Runtime is running but required outputs are degraded")

    def test_verified_delivery_overrides_a_healthy_local_feed(self):
        status, reason = _public_status_summary(
            {
                "running": True,
                "branch_health": {"icecast": True},
                "delivery_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            },
            {"running": True, "last_error": ""},
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(reason, "Runtime is running but required outputs are degraded")

    def test_all_required_outputs_healthy_is_live(self):
        status, reason = _public_status_summary(
            {
                "running": True,
                "branch_health": {"icecast": True, "local": True},
                "required_outputs": {"icecast": True, "local": True},
            },
            {"running": True, "last_error": ""},
        )

        self.assertEqual(status, "live")
        self.assertEqual(reason, "Runtime healthy")

    def test_unreachable_icecast_origin_is_not_reported_live(self):
        status, reason = _public_status_summary(
            {
                "running": True,
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            },
            {"running": True, "last_error": ""},
            icecast_origin_confirmed=False,
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(
            reason,
            "Playout is running but the public mount did not deliver audio bytes",
        )

    def test_failed_mount_is_reported_when_program_survives_output_recovery(self):
        status, reason = _public_status_summary(
            {
                "running": False,
                "program_running": True,
                "branch_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            },
            {"running": True, "last_error": ""},
            icecast_origin_confirmed=False,
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(
            reason,
            "Playout is running but the public mount did not deliver audio bytes",
        )

    def test_silence_floor_does_not_hide_stalled_program_audio(self):
        status, reason = _public_status_summary(
            {
                "running": False,
                "program_running": True,
                "program_pcm_stalled": True,
                "branch_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            },
            {"running": True, "last_error": ""},
            icecast_origin_confirmed=True,
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(
            reason,
            "Playout process is running but program audio stopped advancing",
        )

    def test_continuity_silence_is_not_reported_as_a_live_song(self):
        status, reason = _public_status_summary(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "silence://continuous",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            },
            {"running": True, "last_error": ""},
            icecast_origin_confirmed=True,
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(
            reason,
            "Continuity fallback is active; no program audio is playing",
        )

    @patch("app.services.audio_stream_probe.urlopen")
    def test_icecast_origin_probe_uses_short_lived_cache(self, urlopen):
        urlopen.return_value = _ProbeResponse()
        output = {
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/lofi",
        }

        with patch("app.api.public.time.monotonic", return_value=10.0):
            self.assertFalse(_probe_icecast_origin(2, output, {}))
        with patch("app.api.public.time.monotonic", return_value=12.0):
            self.assertFalse(_probe_icecast_origin(2, output, {}))
        with patch("app.api.public.time.monotonic", return_value=16.0):
            self.assertTrue(_probe_icecast_origin(2, output, {}))

        self.assertEqual(urlopen.call_count, 2)

    @patch("app.services.audio_stream_probe.urlopen")
    def test_icecast_origin_probe_prefers_configured_public_listener(self, urlopen):
        urlopen.return_value = _ProbeResponse()
        output = {
            "icecast_enabled": True,
            "icecast_host": "10.98.98.75",
            "icecast_port": 11154,
            "icecast_mount": "/lofi",
        }

        self.assertFalse(
            _probe_icecast_origin(
                2,
                output,
                {},
                "https://stream.radiotedu.com",
            )
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://stream.radiotedu.com/lofi")

    @patch("app.services.audio_stream_probe.urlopen")
    def test_icecast_origin_probe_requires_two_consecutive_failures(self, urlopen):
        output = {
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/lofi",
        }
        urlopen.side_effect = [
            _ProbeResponse(),
            _ProbeResponse(),
            OSError("reset"),
            OSError("reset"),
        ]

        with patch("app.api.public.time.monotonic", return_value=10.0):
            self.assertFalse(_probe_icecast_origin(2, output, {}))
        with patch("app.api.public.time.monotonic", return_value=16.0):
            self.assertTrue(_probe_icecast_origin(2, output, {}))
        with patch("app.api.public.time.monotonic", return_value=22.0):
            self.assertTrue(_probe_icecast_origin(2, output, {}))
        with patch("app.api.public.time.monotonic", return_value=28.0):
            self.assertFalse(_probe_icecast_origin(2, output, {}))

    @patch("app.services.audio_stream_probe.urlopen")
    def test_icecast_origin_probe_rejects_audio_headers_without_bytes(self, urlopen):
        urlopen.return_value = _ProbeResponse(payload=b"")
        output = {
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/lofi",
        }

        self.assertFalse(_probe_icecast_origin(2, output, {}))

    @patch("app.services.audio_stream_probe.urlopen")
    def test_icecast_origin_probe_rejects_non_audio_content(self, urlopen):
        urlopen.return_value = _ProbeResponse(
            content_type="text/plain; charset=utf-8",
            payload=b"x",
        )
        output = {
            "icecast_enabled": True,
            "icecast_host": "127.0.0.1",
            "icecast_port": 8000,
            "icecast_mount": "/lofi",
        }

        self.assertFalse(_probe_icecast_origin(2, output, {}))


if __name__ == "__main__":
    unittest.main()
