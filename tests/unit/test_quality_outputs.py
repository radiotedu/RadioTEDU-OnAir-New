from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from app.api.streaming import (
    _origin_capacity_diagnostics,
    _refresh_quality_music_runtimes,
    _quality_runtime_diagnostics,
)

from app.services.quality_outputs import (
    QUALITY_CHANNEL_BY_ID,
    QUALITY_CHANNELS,
    QUALITY_PROFILES,
    default_quality_outputs,
    external_settings_key,
    match_music_channels,
    quality_variant_state,
    quality_suffixes_for_channel,
    replace_quality_outputs,
    serialized_outputs,
    public_channel_payload,
)


class QualityOutputsTests(unittest.TestCase):
    def test_apply_hot_refreshes_outputs_without_restarting_programme(self):
        runtime = Mock()
        runtime.refresh_output_settings.return_value = {
            "producer_preserved": True,
        }
        channels = [
            {
                "channel_id": "lofi",
                "station_id": 2,
                "external": False,
            }
        ]

        with patch("app.api.runtime.runtime_registry", runtime):
            result = _refresh_quality_music_runtimes(channels)

        runtime.refresh_output_settings.assert_called_once_with(2)
        self.assertEqual(
            result,
            [
                {
                    "channel_id": "lofi",
                    "station_id": 2,
                    "ok": True,
                    "producer_preserved": True,
                }
            ],
        )

    def test_apply_rejects_a_refresh_that_restarts_programme(self):
        runtime = Mock()
        runtime.refresh_output_settings.return_value = {
            "producer_preserved": False,
        }
        channels = [
            {
                "channel_id": "lofi",
                "station_id": 2,
                "external": False,
            }
        ]

        with patch("app.api.runtime.runtime_registry", runtime):
            result = _refresh_quality_music_runtimes(channels)

        self.assertEqual(result[0]["ok"], False)
        self.assertEqual(result[0]["error_code"], "runtime_refresh_failed")

    def test_runtime_diagnostics_require_verified_mount_delivery(self):
        runtime = Mock()
        runtime.status.return_value = {
            "running": True,
            "branch_health": {"icecast": True, "icecast:/lofi-low": True},
            "delivery_health": {"icecast": True, "icecast:/lofi-low": False},
        }
        channels = [
            {
                "channel_id": "lofi",
                "station_id": 2,
                "external": False,
                "primary": {"enabled": True, "mount": "/lofi"},
                "variants": [{"mount": "/lofi-low", "enabled": True}],
            }
        ]

        with patch("app.api.runtime.runtime_registry", runtime):
            result = _quality_runtime_diagnostics(channels)

        self.assertEqual(result[0]["healthy_branches"], ["icecast"])
        self.assertEqual(result[0]["unhealthy_branches"], ["icecast:/lofi-low"])
        self.assertEqual(result[0]["health_basis"], "verified_delivery")

    def test_declared_capacity_is_not_verified_when_delivery_is_partial(self):
        runtime = []
        for index, channel in enumerate(QUALITY_CHANNELS):
            expected = [
                "icecast",
                *[
                    f"icecast:{channel.base_mount}-{suffix}"
                    for suffix in quality_suffixes_for_channel(channel)
                ],
            ]
            healthy = ["icecast"] if index < 4 else []
            runtime.append(
                {
                    "owner": "onair_station_runtime",
                    "runtime_checked": True,
                    "expected_branches": expected,
                    "healthy_branches": healthy,
                    "unhealthy_branches": [item for item in expected if item not in healthy],
                }
            )

        result = _origin_capacity_diagnostics(20, runtime, 14)

        self.assertTrue(result["configured_sufficient"])
        self.assertEqual(result["observed_healthy_local_mounts"], 4)
        self.assertEqual(result["observed_unhealthy_local_mounts"], 10)
        self.assertFalse(result["verified"])
        self.assertIn("accepted 4 of 14", result["warning"])

    def test_capacity_is_verified_only_after_every_local_mount_delivers(self):
        runtime = []
        for channel in QUALITY_CHANNELS:
            expected = [
                "icecast",
                *[
                    f"icecast:{channel.base_mount}-{suffix}"
                    for suffix in quality_suffixes_for_channel(channel)
                ],
            ]
            runtime.append(
                {
                    "owner": "onair_station_runtime",
                    "runtime_checked": True,
                    "expected_branches": expected,
                    "healthy_branches": expected,
                    "unhealthy_branches": [],
                }
            )

        result = _origin_capacity_diagnostics(20, runtime, 14)

        self.assertEqual(result["verification_basis"], "verified_mount_delivery")
        self.assertEqual(result["observed_healthy_local_mounts"], 14)
        self.assertTrue(result["verified"])
        self.assertEqual(result["warning"], "")

    def test_canonical_mount_wins_over_a_similarly_named_inactive_station(self):
        matched = match_music_channels(
            [
                {"id": 7, "name": "RadioTEDU", "_icecast_mount": ""},
                {"id": 4, "name": "RadioTEDU Pop", "_icecast_mount": "/radio"},
            ]
        )

        self.assertEqual(matched["radio"]["id"], 4)

    def test_every_channel_has_only_approved_outputs_without_credentials(self):
        for channel in QUALITY_CHANNEL_BY_ID.values():
            outputs = default_quality_outputs(channel)
            suffixes = quality_suffixes_for_channel(channel)
            self.assertEqual([item["quality"] for item in outputs], list(suffixes))
            self.assertEqual(
                [item["icecast_mount"] for item in outputs],
                [f"{channel.base_mount}-{suffix}" for suffix in suffixes],
            )
            for output in outputs:
                self.assertNotIn("password", output)
                self.assertNotIn("icecast_password", output)
                self.assertNotIn("icecast_user", output)
                self.assertNotIn("icecast_host", output)
                self.assertEqual(output["credential_mode"], "inherit_legacy_output")
                self.assertTrue(output["metadata_suppressed"])

    def test_profiles_are_exact_required_targets(self):
        self.assertEqual(set(QUALITY_PROFILES), {"low", "flac"})
        self.assertEqual(QUALITY_PROFILES["low"]["stream_bitrate_kbps"], 32)
        self.assertEqual(QUALITY_PROFILES["flac"]["stream_bitrate_kbps"], 0)
        self.assertEqual(QUALITY_PROFILES["low"]["stream_codec_profile"], "opus_32")
        self.assertEqual(QUALITY_PROFILES["low"]["codec"], "Opus")
        self.assertEqual(
            QUALITY_PROFILES["flac"]["stream_codec_profile"],
            "ogg_flac_lossless",
        )

    def test_normal_mount_is_the_recommended_default_for_consumers(self):
        channel = QUALITY_CHANNEL_BY_ID["lofi"]
        payload = public_channel_payload(
            channel,
            variants=quality_variant_state(channel, []),
            station_id=2,
            credential_configured=True,
            credential_status="ready",
            applied_by="test",
        )

        self.assertEqual(payload["recommended_quality"], "normal")
        self.assertEqual(payload["recommended_mount"], "/lofi")

    def test_replacing_quality_outputs_preserves_unrelated_outputs_and_legacy_mount(self):
        channel = QUALITY_CHANNEL_BY_ID["lofi"]
        existing = [
            {"mount": "/backup", "password": "preserve-existing-generic-output"},
            {"mount": "/lofi-low", "enabled": False, "bitrate_kbps": 12},
        ]

        updated = replace_quality_outputs(
            channel,
            existing,
            variants={"low": {"enabled": False}},
        )

        self.assertEqual(updated[0], existing[0])
        self.assertNotIn("/lofi", [item.get("icecast_mount") for item in updated])
        qualities = {item["quality"]: item for item in updated[1:]}
        self.assertFalse(qualities["low"]["enabled"])
        self.assertEqual(qualities["low"]["stream_bitrate_kbps"], 32)
        self.assertEqual(len(qualities), 1)

    def test_nonapproved_flac_variant_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported quality variants: flac"):
            replace_quality_outputs(
                QUALITY_CHANNEL_BY_ID["lofi"],
                [],
                variants={"flac": {"enabled": True}},
            )

    def test_serialized_quality_outputs_are_secret_free(self):
        raw = serialized_outputs(default_quality_outputs(QUALITY_CHANNEL_BY_ID["classic"]))
        parsed = json.loads(raw)

        self.assertEqual(len(parsed), 2)
        self.assertNotIn("password", raw.lower())
        self.assertNotIn("secret", raw.lower())

    def test_music_station_matching_excludes_events_and_selects_primary_radiotedu(self):
        stations = [
            {"id": 1, "name": "RadioTEDU Classical"},
            {"id": 2, "name": "RadioTEDU Lo-Fi"},
            {"id": 4, "name": "RadioTEDU Events"},
            {"id": 5, "name": "RadioTEDU Jazz"},
            {"id": 7, "name": "RadioTEDU"},
            {"id": 8, "name": "Rock"},
            {"id": 9, "name": "RadioTEDU Energetic"},
        ]

        matched = match_music_channels(stations)

        self.assertEqual(set(matched), {"classic", "lofi", "cazz", "energize", "radio", "rock"})
        self.assertEqual(matched["radio"]["id"], 7)
        self.assertNotIn(4, {int(item["id"]) for item in matched.values()})

    def test_variant_state_round_trips_enabled_and_public_flags(self):
        channel = QUALITY_CHANNEL_BY_ID["classic"]
        outputs = replace_quality_outputs(
            channel,
            [],
            variants={"flac": {"enabled": False, "icecast_public": False}},
        )

        state = quality_variant_state(channel, outputs)

        self.assertFalse(state["flac"]["enabled"])
        self.assertFalse(state["flac"]["icecast_public"])
        self.assertEqual(set(state), {"low", "flac"})

    def test_string_false_flags_remain_disabled_after_import(self):
        channel = QUALITY_CHANNEL_BY_ID["lofi"]
        state = quality_variant_state(
            channel,
            [
                {
                    "mount": "/lofi-low",
                    "enabled": "false",
                    "icecast_public": "false",
                }
            ],
        )

        self.assertFalse(state["low"]["enabled"])
        self.assertFalse(state["low"]["icecast_public"])

    def test_external_setting_key_helper_is_stable_for_legacy_data(self):
        self.assertEqual(
            external_settings_key("en"),
            "quality_outputs_external_en",
        )
        self.assertEqual(
            external_settings_key("fr"),
            "quality_outputs_external_fr",
        )

    def test_ai_mounts_are_not_in_the_quality_catalog_without_origin_mounts(self):
        self.assertNotIn("en", QUALITY_CHANNEL_BY_ID)
        self.assertNotIn("fr", QUALITY_CHANNEL_BY_ID)

    def test_new_outputs_are_safe_disabled_until_apply_gates_pass(self):
        outputs = default_quality_outputs(QUALITY_CHANNEL_BY_ID["classic"])
        self.assertTrue(all(not item["enabled"] for item in outputs))


if __name__ == "__main__":
    unittest.main()
