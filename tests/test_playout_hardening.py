import sys
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_internal"))
sys.path.insert(0, str(ROOT))

from app.audio.station_runtime import StationRuntime  # noqa: E402
from app.audio.icecast_audio_sink import IcecastAudioSink  # noqa: E402
from app.audio.gst_pipeline import StationPipelineConfig, resolve_stream_profile  # noqa: E402
from app.audio.mic_session import MicSession  # noqa: E402
from app.api import ai_host as ai_host_api  # noqa: E402
from app.api import ads as ads_api  # noqa: E402
from app.api import legacy as legacy_api  # noqa: E402
from app.api import streaming as streaming_api  # noqa: E402
from app.api import tracks as tracks_api  # noqa: E402
from app import db as app_db  # noqa: E402
from app.services import ai_host as ai_host_module  # noqa: E402
from app.services.ai_host import AIHostService  # noqa: E402
from app.engine.ad_policy import (  # noqa: E402
    ads_enabled_from_settings,
    rocket_ad_insertion_enabled_from_settings,
)
from app.engine import runtime_registry as runtime_registry_module  # noqa: E402
from app.engine import station_worker as station_worker_module  # noqa: E402
from app.engine.runtime_registry import StationRuntimeRegistry  # noqa: E402
from app.engine.runtime_supervisor import RuntimeSupervisor  # noqa: E402
from app.engine.station_worker import StationWorker  # noqa: E402
from app.engine.worker_loop import StationWorkerLoopManager  # noqa: E402
from app.repositories.schedule_repo import ScheduleRepository  # noqa: E402
from fastapi import HTTPException  # noqa: E402
import radiotedu_playout_guard as playout_guard  # noqa: E402


def source_file(*parts: str) -> Path:
    source_path = ROOT.joinpath(*parts)
    if source_path.exists():
        return source_path
    return ROOT.joinpath("_internal", *parts)


def started_seconds_ago(seconds: float) -> str:
    return (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")


class FakeQueueRepo:
    def __init__(self, playing=None, pending=None):
        self.playing = playing
        self.pending = pending
        self.done = []
        self.failed = []

    def current_playing(self, _station_id):
        return self.playing

    def next_pending(self, _station_id):
        return self.pending

    def mark_done(self, item_id):
        self.done.append(int(item_id))

    def mark_failed(self, item_id):
        self.failed.append(int(item_id))


class FakeAdRepo:
    def __init__(self, playing=None, pending=None, active=None):
        self.playing = playing
        self.pending = pending
        self.active = list(active) if active is not None else [
            row for row in (playing, pending) if row is not None
        ]
        self.failed = []
        self.done = []
        self.playing_ids = []
        self.enqueued = []

    def current_playing(self, _station_id):
        return self.playing

    def next_due(self, _station_id):
        return self.pending

    def list_active(self, _station_id, limit=100):
        return list(self.active)[: int(limit)]

    def enqueue(self, station_id, track_id, due_at, priority=0, dedupe_key=None):
        item_id = len(self.enqueued) + 1
        self.enqueued.append(
            {
                "id": item_id,
                "station_id": int(station_id),
                "track_id": int(track_id),
                "due_at": str(due_at),
                "priority": int(priority),
                "dedupe_key": dedupe_key,
            }
        )
        return item_id

    def mark_playing(self, item_id):
        self.playing_ids.append(int(item_id))

    def mark_failed(self, item_id):
        item_id = int(item_id)
        self.failed.append(item_id)
        self.active = [
            row for row in self.active if int(row.get("id", 0) or 0) != item_id
        ]
        if self.playing and int(self.playing.get("id", 0) or 0) == item_id:
            self.playing = None
        if self.pending and int(self.pending.get("id", 0) or 0) == item_id:
            self.pending = None

    def mark_done(self, item_id):
        self.done.append(int(item_id))


class FakePlayoutState:
    def __init__(self):
        self.values = []

    def set_current(self, station_id, source, item_id):
        self.values.append((int(station_id), str(source), item_id))


class FakeRuntimeRegistry:
    def __init__(self, status):
        self._status = dict(status)
        self.starts = []

    def status(self, _station_id):
        return dict(self._status)

    def start_station(self, station_id, input_uri, **kwargs):
        self.starts.append((int(station_id), str(input_uri), dict(kwargs)))
        self._status.update(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": str(input_uri),
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, chunk):
        self.writes.append(bytes(chunk))

    def flush(self):
        return None


class FakeSink:
    def __init__(self):
        self.stdin = FakeStdin()

    def is_running(self):
        return True


class FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def read(self, *_args):
        return b""

    def readline(self, *_args):
        return b""


class FakeProc:
    _next_pid = 1000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.stdin = FakePipe()
        self.stdout = FakePipe()
        self.stderr = FakePipe()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return None if self.returncode is None else self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.terminated = True
        self.returncode = -9


class FakeIcecastSource:
    def __init__(self, _cfg):
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(bytes(payload))

    def close(self):
        self.closed = True


class FakeFloorRuntime:
    def __init__(self, status):
        self._status = dict(status)

    def status(self):
        return dict(self._status)

    def is_running(self):
        return bool(self._status.get("program_running", False))

    def branch_health(self):
        return dict(self._status.get("branch_health") or {})


class FakeSupervisorRegistry:
    def __init__(self, status):
        self._status = dict(status)
        self.stop_calls = []

    def status(self, _station_id):
        return dict(self._status)

    def stop_station(self, station_id):
        self.stop_calls.append(int(station_id))


def make_worker(runtime_status):
    worker = object.__new__(StationWorker)
    worker.station_id = 1
    worker.runtime_registry = FakeRuntimeRegistry(runtime_status)
    worker.playout_state = FakePlayoutState()
    worker._ads_enabled = lambda: True
    worker._broadcast_worker_state = lambda **_kwargs: None
    worker._start_continuity_fallback = lambda **kwargs: {
        "source": "fallback",
        **kwargs,
    }
    return worker


class PlayoutHardeningTests(unittest.TestCase):
    def setUp(self):
        station_worker_module._RESTART_SUPPRESSION.clear()

    def test_radio_mount_repair_only_updates_exact_radio_mount(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        cur.execute(
            "CREATE TABLE station_outputs (station_id INTEGER PRIMARY KEY, icecast_mount TEXT NOT NULL)"
        )
        cur.execute(
            "CREATE TABLE station_settings (station_id INTEGER, key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        cur.executemany(
            "INSERT INTO stations (id, name) VALUES (?, ?)",
            [(1, "RadioTEDU Main"), (2, "RadioTEDU Lo-Fi"), (3, "RadioTEDU Already")],
        )
        cur.executemany(
            "INSERT INTO station_outputs (station_id, icecast_mount) VALUES (?, ?)",
            [(1, "/radio"), (2, "/lofi"), (3, "/radio1")],
        )
        cur.executemany(
            "INSERT INTO station_settings (station_id, key, value) VALUES (?, ?, ?)",
            [
                (1, "icecast_mount", "/radio"),
                (1, "icecast_url", "http://stream.example.test:8000/radio"),
                (2, "icecast_mount", "/lofi"),
                (2, "icecast_url", "http://stream.example.test:8000/lofi"),
                (3, "icecast_mount", "/radio1"),
            ],
        )

        app_db._migrate_station_outputs(cur)

        outputs = dict(cur.execute("SELECT station_id, icecast_mount FROM station_outputs"))
        self.assertEqual(outputs[1], "/radio")
        self.assertEqual(outputs[2], "/lofi")
        self.assertEqual(outputs[3], "/radio1")

    def _read_app_js(self):
        return source_file("app", "static", "onair", "app.js").read_text(
            encoding="utf-8"
        )

    def _extract_js_function(self, app_js, name):
        marker = f"async function {name}"
        start = app_js.find(marker)
        if start < 0:
            marker = f"function {name}"
            start = app_js.find(marker)
        self.assertGreaterEqual(start, 0, f"{name} not found")
        signature_open = app_js.find("(", start)
        self.assertGreaterEqual(signature_open, 0, f"{name} has no signature")
        signature_depth = 0
        signature_close = -1
        for idx in range(signature_open, len(app_js)):
            char = app_js[idx]
            if char == "(":
                signature_depth += 1
            elif char == ")":
                signature_depth -= 1
                if signature_depth == 0:
                    signature_close = idx
                    break
        self.assertGreaterEqual(signature_close, 0, f"{name} signature was not closed")
        brace = app_js.find("{", signature_close)
        self.assertGreaterEqual(brace, 0, f"{name} has no body")
        depth = 0
        for idx in range(brace, len(app_js)):
            char = app_js[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return app_js[start : idx + 1]
        self.fail(f"{name} body was not closed")

    def test_ad_policy_defaults_disabled_until_explicitly_enabled(self):
        self.assertFalse(ads_enabled_from_settings({}))
        self.assertFalse(ads_enabled_from_settings({"hourly_ad_enabled": "false"}))
        self.assertFalse(ads_enabled_from_settings({"hourly_ad_v2_enabled": "false"}))
        self.assertTrue(ads_enabled_from_settings({"hourly_ad_enabled": "true"}))
        self.assertTrue(
            ads_enabled_from_settings(
                {"hourly_ad_enabled": "true", "hourly_ad_v2_enabled": "false"}
            )
        )
        self.assertTrue(ads_enabled_from_settings({"hourly_ad_v2_enabled": "true"}))
        self.assertFalse(rocket_ad_insertion_enabled_from_settings({}))
        self.assertFalse(
            rocket_ad_insertion_enabled_from_settings(
                {"rocket_ad_insertion_enabled": "false"}
            )
        )
        self.assertTrue(
            rocket_ad_insertion_enabled_from_settings(
                {"rocket_ad_insertion_enabled": "true"}
            )
        )

    def test_disabled_ads_prevent_hourly_scheduling(self):
        worker = make_worker({"running": True})
        worker.ad_repo = FakeAdRepo()
        worker._get_hourly_ad_settings = lambda: {
            "enabled": False,
            "interval_minutes": 60,
            "ad_count": [],
        }
        worker._pick_hourly_ads = (
            lambda: (_ for _ in ()).throw(AssertionError("ads were picked"))
        )

        worker._ensure_hourly_ad_break()

        self.assertEqual(worker.ad_repo.enqueued, [])

    def test_disabled_ads_fail_stale_active_rows(self):
        active = [
            {"id": 30, "track_id": 201, "status": "playing"},
            {"id": 31, "track_id": 202, "status": "pending"},
        ]
        worker = make_worker({"running": True})
        worker.conn = None
        worker.ad_repo = FakeAdRepo(active=active)
        worker._ads_enabled = lambda: False

        self.assertEqual(worker._fail_disabled_active_ads(), 2)
        self.assertEqual(worker.ad_repo.failed, [30, 31])
        self.assertIn((1, "none", None), worker.playout_state.values)

    def test_disabled_ads_block_due_ad_playback(self):
        due_ad = {"id": 40, "track_id": 203, "status": "pending"}
        worker = make_worker({"running": True})
        worker.conn = None
        worker.ad_repo = FakeAdRepo(pending=due_ad)
        worker._ads_enabled = lambda: False

        self.assertIsNone(worker._next_due_ad_if_allowed())
        self.assertEqual(worker.ad_repo.failed, [40])

    def test_disabled_ads_stop_playing_ad_continuation(self):
        playing = {"id": 41, "track_id": 204, "status": "playing"}
        fallback_calls = []
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://ad",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.conn = None
        worker.ad_repo = FakeAdRepo(playing=playing)
        worker._ads_enabled = lambda: False
        worker._start_continuity_fallback = (
            lambda **kwargs: fallback_calls.append(dict(kwargs)) or {"source": "fallback"}
        )

        self.assertTrue(worker._advance_playing_ad_item())
        self.assertEqual(worker.ad_repo.failed, [41])
        self.assertEqual(fallback_calls[0]["reason"], "ads_disabled_for_station")

    def test_manual_ad_enqueue_rejected_when_ads_disabled(self):
        original_init_db = ads_api.init_db
        original_get_connection = ads_api.get_connection
        original_station_ads_enabled = ads_api.station_ads_enabled

        class FakeConn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        conn = FakeConn()
        try:
            ads_api.init_db = lambda: None
            ads_api.get_connection = lambda: conn
            ads_api.station_ads_enabled = lambda _conn, _station_id: False

            with self.assertRaises(HTTPException) as raised:
                ads_api.create_ad_item(
                    ads_api.AdItemCreate(
                        station_id=1,
                        track_id=200,
                        due_at="2026-05-15 00:00:00",
                    )
                )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.detail, "ads_disabled_for_station")
            self.assertTrue(conn.closed)
        finally:
            ads_api.init_db = original_init_db
            ads_api.get_connection = original_get_connection
            ads_api.station_ads_enabled = original_station_ads_enabled

    def test_rocket_ad_policy_blocks_midroll_but_not_metadata(self):
        original_init_db = streaming_api.init_db
        original_get_connection = streaming_api.get_connection
        original_settings_repo = streaming_api.SettingsRepository
        original_mount_credentials = streaming_api._mount_credentials
        original_request_text = streaming_api._request_text

        class FakeConn:
            def close(self):
                return None

        class FakeSettingsRepository:
            def __init__(self, _conn):
                return None

            def get_system(self):
                return {"rocket_ad_insertion_enabled": "false"}

            def get_station(self, _station_id):
                return {"rocket_ad_insertion_enabled": "false"}

        try:
            streaming_api.init_db = lambda: None
            streaming_api.get_connection = lambda: FakeConn()
            streaming_api.SettingsRepository = FakeSettingsRepository
            streaming_api._mount_credentials = lambda *_args, **_kwargs: {
                "host": "127.0.0.1",
                "port": 8000,
                "user": "source",
                "password": "pw",
            }
            streaming_api._request_text = lambda *_args, **_kwargs: {"ok": True}

            metadata_result = streaming_api.update_stream_metadata(
                streaming_api.MetadataUpdatePayload(station_id=5, mount="/cazz", song="Test")
            )
            self.assertEqual(metadata_result, {"ok": True})

            with self.assertRaises(HTTPException) as raised:
                streaming_api.insert_midroll(
                    streaming_api.MidrollPayload(
                        station_id=5,
                        mount="/cazz",
                        ads=[{"duration": 1}],
                    )
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.detail, "ads_disabled_for_station")
        finally:
            streaming_api.init_db = original_init_db
            streaming_api.get_connection = original_get_connection
            streaming_api.SettingsRepository = original_settings_repo
            streaming_api._mount_credentials = original_mount_credentials
            streaming_api._request_text = original_request_text

    def test_opus_profile_uses_stream_stable_constrained_vbr(self):
        profile = resolve_stream_profile("opus_192", 192)

        self.assertEqual(profile["codec"], "opus")
        self.assertEqual(profile["content_type"], "audio/ogg")
        self.assertEqual(profile["format"], "ogg")
        self.assertEqual(profile["ffmpeg_codec"], "libopus")
        self.assertEqual(profile["bitrate_kbps"], 192)
        self.assertIn("-vbr", profile["ffmpeg_encoder_args"])
        vbr_index = profile["ffmpeg_encoder_args"].index("-vbr")
        self.assertEqual(profile["ffmpeg_encoder_args"][vbr_index + 1], "constrained")

        max_profile = resolve_stream_profile("opus_320", 196)
        self.assertEqual(max_profile["codec"], "opus")
        self.assertEqual(max_profile["content_type"], "audio/ogg")
        self.assertEqual(max_profile["format"], "ogg")
        self.assertEqual(max_profile["bitrate_kbps"], 320)

    def test_ytdlp_import_rejects_non_url_before_queueing(self):
        jobs_before = len(legacy_api._YTDLP_JOBS)
        pending_before = list(legacy_api._YTDLP_PENDING_JOB_IDS)

        with self.assertRaises(HTTPException) as raised:
            legacy_api.legacy_queue_ytdlp_import(
                legacy_api.YtDlpImportPayload(url="not-a-valid-url"),
                _user={"id": 1, "role": "superadmin"},
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "valid URL is required")
        self.assertEqual(len(legacy_api._YTDLP_JOBS), jobs_before)
        self.assertEqual(legacy_api._YTDLP_PENDING_JOB_IDS, pending_before)

    def test_late_stale_metadata_push_reasserts_latest_generation(self):
        registry = StationRuntimeRegistry(runtime_factory=lambda _station_id: None)
        old_cfg = StationPipelineConfig(
            input_uri="test://old",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Old Song",
            stream_artist="Artist",
        )
        new_cfg = StationPipelineConfig(
            input_uri="test://new",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="New Song",
            stream_artist="Artist",
        )
        generation = registry._next_metadata_generation(1)
        scheduled = []
        original_send = runtime_registry_module._send_icecast_metadata

        def fake_send(cfg, **_kwargs):
            self.assertEqual(cfg.stream_title, "Old Song")
            registry._last_metadata_cfg[1] = new_cfg
            registry._next_metadata_generation(1)
            registry._metadata_sent[1] = "Artist - New Song"
            return True

        try:
            runtime_registry_module._send_icecast_metadata = fake_send
            registry._schedule_latest_metadata_update = lambda station_id: scheduled.append(
                int(station_id)
            )

            self.assertFalse(registry._push_metadata_now(1, old_cfg, generation))
        finally:
            runtime_registry_module._send_icecast_metadata = original_send

        self.assertEqual(scheduled, [1])
        self.assertNotIn(1, registry._metadata_sent)

    def test_stale_metadata_retry_cancels_between_attempts(self):
        cfg = StationPipelineConfig(
            input_uri="test://old",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Old Song",
            stream_artist="Artist",
        )
        calls = []
        keep_running = True
        original_urlopen = runtime_registry_module.urlopen
        original_sleep = runtime_registry_module.time.sleep

        def fake_urlopen(_request, timeout=None):
            nonlocal keep_running
            calls.append(timeout)
            keep_running = False
            raise OSError("stale generation")

        try:
            runtime_registry_module.urlopen = fake_urlopen
            runtime_registry_module.time.sleep = lambda _seconds: None

            sent = runtime_registry_module._send_icecast_metadata(
                cfg,
                timeout_seconds=0.01,
                should_continue=lambda: keep_running,
            )
        finally:
            runtime_registry_module.urlopen = original_urlopen
            runtime_registry_module.time.sleep = original_sleep

        self.assertFalse(sent)
        self.assertEqual(len(calls), 1)

    def test_metadata_sender_respects_tls_enabled(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="stream.example.test",
            icecast_port=443,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            icecast_tls_enabled=True,
            stream_title="Song",
            stream_artist="Artist",
        )
        urls = []
        original_urlopen = runtime_registry_module.urlopen

        def fake_urlopen(request, timeout=None):
            urls.append(str(getattr(request, "full_url", "")))
            return FakeResponse()

        try:
            runtime_registry_module.urlopen = fake_urlopen

            self.assertTrue(runtime_registry_module._send_icecast_metadata(cfg))
        finally:
            runtime_registry_module.urlopen = original_urlopen

        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("https://stream.example.test:443/admin/metadata?"))

    def test_metadata_delivery_failure_is_exposed_in_runtime_status(self):
        registry = StationRuntimeRegistry(runtime_factory=lambda *_args: object())
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Song",
            stream_artist="Artist",
        )
        original_send = runtime_registry_module._send_icecast_metadata

        def fake_send(_cfg, **kwargs):
            kwargs["on_result"](
                {
                    "ok": False,
                    "mount": "/station1",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "scheme": "http",
                    "attempts": 4,
                    "status": None,
                    "error": "Remote end closed connection without response",
                }
            )
            return False

        try:
            runtime_registry_module._send_icecast_metadata = fake_send

            self.assertFalse(registry._push_metadata_now(1, cfg))
        finally:
            runtime_registry_module._send_icecast_metadata = original_send

        delivery = registry.status(1)["metadata_delivery"]
        self.assertFalse(delivery["ok"])
        self.assertEqual(delivery["song"], "Artist - Song")
        self.assertIn("Remote end closed", delivery["last_error"])
        self.assertEqual(delivery["outputs"][0]["mount"], "/station1")
        self.assertEqual(delivery["retry"]["failure_count"], 1)
        self.assertGreater(delivery["retry"]["retry_in_seconds"], 0)
        self.assertFalse(registry._metadata_retry_ready(1))

        registry._metadata_retry_state[1]["next_retry_monotonic"] = 0
        self.assertTrue(registry._metadata_retry_ready(1))

    def test_metadata_send_requires_each_configured_output(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Song",
            stream_artist="Artist",
            extra_icecast_outputs=(
                {
                    "enabled": True,
                    "icecast_host": "127.0.0.1",
                    "icecast_port": 8001,
                    "icecast_mount": "/backup",
                    "icecast_user": "source",
                    "icecast_password": "pw",
                },
            ),
        )
        calls = []
        original_urlopen = runtime_registry_module.urlopen
        original_sleep = runtime_registry_module.time.sleep

        def fake_urlopen(request, timeout=None):
            url = str(getattr(request, "full_url", ""))
            calls.append(url)
            if "mount=/backup" in url:
                raise OSError("backup down")
            return FakeResponse()

        try:
            runtime_registry_module.urlopen = fake_urlopen
            runtime_registry_module.time.sleep = lambda _seconds: None

            sent = runtime_registry_module._send_icecast_metadata(cfg)
        finally:
            runtime_registry_module.urlopen = original_urlopen
            runtime_registry_module.time.sleep = original_sleep

        self.assertFalse(sent)
        self.assertEqual(sum("mount=/station1" in url for url in calls), 1)
        self.assertEqual(sum("mount=/backup" in url for url in calls), 4)

    def test_metadata_worker_is_single_per_station(self):
        original_thread = runtime_registry_module.threading.Thread
        created = []

        class FakeThread:
            def __init__(self, *, target, args=(), name="", daemon=False):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                created.append(self)

        try:
            runtime_registry_module.threading.Thread = FakeThread
            registry = StationRuntimeRegistry(runtime_factory=lambda _station_id: None)

            registry._wake_metadata_worker(1)
            registry._wake_metadata_worker(1)
            registry._wake_metadata_worker(2)
        finally:
            runtime_registry_module.threading.Thread = original_thread

        self.assertEqual([thread.name for thread in created], ["icecast-metadata-1", "icecast-metadata-2"])

    def test_metadata_worker_restarts_with_latest_generation(self):
        registry = StationRuntimeRegistry(runtime_factory=lambda _station_id: None)
        event = runtime_registry_module.threading.Event()
        old_cfg = StationPipelineConfig(
            input_uri="test://old",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="Old Song",
            stream_artist="Artist",
        )
        new_cfg = StationPipelineConfig(
            input_uri="test://new",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/station1",
            icecast_user="source",
            icecast_password="pw",
            local_output_enabled=False,
            output_device_id="",
            icecast_enabled=True,
            stream_title="New Song",
            stream_artist="Artist",
        )
        registry._last_metadata_cfg[1] = old_cfg
        registry._next_metadata_generation(1)
        sent = []

        def fake_push(station_id, cfg, generation):
            sent.append((int(station_id), str(cfg.stream_title), int(generation)))
            if str(cfg.stream_title) == "Old Song":
                registry._last_metadata_cfg[1] = new_cfg
                registry._next_metadata_generation(1)
                event.set()
                return False
            return True

        registry._push_metadata_now = fake_push

        registry._push_latest_metadata_for_station(1, event)

        self.assertEqual(
            [(station_id, title) for station_id, title, _generation in sent],
            [(1, "Old Song"), (1, "New Song")],
        )

    def test_music_duration_does_not_cut_active_runtime_for_tts(self):
        playing = {
            "id": 10,
            "track_id": 101,
            "started_at": started_seconds_ago(12),
            "duration": 10.0,
            "track_type": "music",
        }
        pending = {"track_type": "announcement"}
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://song",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending=pending)
        worker._track_runtime_fields = lambda _track_id: (
            "test://song",
            "Song",
            "Artist",
            "music",
        )

        self.assertFalse(worker._advance_playing_queue_item())
        self.assertEqual(worker.queue_repo.done, [])

    def test_runtime_dies_early_restarts_current_item_not_next(self):
        playing = {
            "id": 11,
            "track_id": 102,
            "started_at": started_seconds_ago(5),
            "duration": 100.0,
            "track_type": "music",
        }
        worker = make_worker(
            {
                "running": False,
                "program_running": False,
                "active_input_uri": "",
                "branch_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending={"track_type": "announcement"})
        worker._track_runtime_fields = lambda _track_id: (
            "test://song",
            "Song",
            "Artist",
            "music",
        )

        self.assertFalse(worker._advance_playing_queue_item())
        self.assertEqual(worker.queue_repo.done, [])
        self.assertEqual(worker.queue_repo.failed, [])
        self.assertEqual(len(worker.runtime_registry.starts), 1)
        resume_offset = worker.runtime_registry.starts[0][2]["start_offset_seconds"]
        self.assertGreater(resume_offset, 4.0)
        self.assertLess(resume_offset, 7.0)

    def test_long_jingle_recovers_when_runtime_has_already_advanced(self):
        playing = {
            "id": 12,
            "track_id": 20391,
            "started_at": started_seconds_ago(1),
            "duration": 10.0,
            "track_type": "jingle",
        }
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://next-song",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending={"track_type": "music"})
        worker._track_runtime_fields = lambda _track_id: (
            "test://radiotedu-jingle",
            "RadioTEDU Sweeper",
            "",
            "jingle",
        )
        worker._complete_queue_item = lambda item: worker.queue_repo.mark_done(item["id"])
        worker._default_crossfade_seconds = lambda: 3.0

        self.assertTrue(worker._advance_playing_queue_item())
        self.assertEqual(worker.queue_repo.done, [])
        self.assertEqual(len(worker.runtime_registry.starts), 1)
        self.assertGreater(worker.runtime_registry.starts[0][2]["start_offset_seconds"], 0.0)

    def test_long_jingle_recovers_when_runtime_ends_between_polls(self):
        playing = {
            "id": 13,
            "track_id": 20391,
            "started_at": started_seconds_ago(1),
            "duration": 10.0,
            "track_type": "jingle",
        }
        worker = make_worker(
            {
                "running": False,
                "program_running": False,
                "active_input_uri": "",
                "branch_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending={"track_type": "music"})
        worker._track_runtime_fields = lambda _track_id: (
            "test://radiotedu-jingle",
            "RadioTEDU Sweeper",
            "",
            "jingle",
        )
        worker._complete_queue_item = lambda item: worker.queue_repo.mark_done(item["id"])
        worker._default_crossfade_seconds = lambda: 3.0

        self.assertFalse(worker._advance_playing_queue_item())
        self.assertEqual(worker.queue_repo.done, [])
        self.assertEqual(len(worker.runtime_registry.starts), 1)

    def test_restart_cooldown_never_fails_current_queue_item(self):
        playing = {
            "id": 15,
            "track_id": 20391,
            "started_at": started_seconds_ago(1),
            "duration": 10.0,
            "track_type": "jingle",
        }
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://wrong-source",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending={"track_type": "music"})
        worker._track_runtime_fields = lambda _track_id: (
            "test://radiotedu-jingle",
            "RadioTEDU Sweeper",
            "",
            "jingle",
        )

        self.assertTrue(worker._restart_playing_queue_item_if_runtime_mismatched(playing))
        worker.runtime_registry._status["active_input_uri"] = "test://still-stale"
        self.assertFalse(worker._restart_playing_queue_item_if_runtime_mismatched(playing))
        self.assertEqual(worker.queue_repo.failed, [])

    def test_short_jingle_is_not_replayed_after_runtime_advanced(self):
        playing = {
            "id": 14,
            "track_id": 20391,
            "started_at": started_seconds_ago(0.25),
            "duration": 1.0,
            "track_type": "jingle",
        }
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://next-song",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.queue_repo = FakeQueueRepo(playing=playing, pending={"track_type": "music"})
        worker._track_runtime_fields = lambda _track_id: (
            "test://radiotedu-jingle",
            "RadioTEDU Sweeper",
            "",
            "jingle",
        )
        worker._complete_queue_item = lambda item: worker.queue_repo.mark_done(item["id"])
        worker._default_crossfade_seconds = lambda: 3.0

        self.assertTrue(worker._advance_playing_queue_item())
        self.assertEqual(worker.queue_repo.done, [14])
        self.assertEqual(worker.runtime_registry.starts, [])

    def test_guard_starts_backend_without_lifespan_startup(self):
        original_popen = playout_guard.subprocess.Popen
        original_python = playout_guard._python_executable
        calls = []

        class FakeProcess:
            returncode = None

        def fake_popen(cmd, **kwargs):
            calls.append((list(cmd), dict(kwargs)))
            return FakeProcess()

        try:
            playout_guard.subprocess.Popen = fake_popen
            playout_guard._python_executable = lambda: "python.exe"

            playout_guard.start_backend()
        finally:
            playout_guard.subprocess.Popen = original_popen
            playout_guard._python_executable = original_python

        self.assertEqual(len(calls), 1)
        cmd = calls[0][0]
        env = calls[0][1].get("env") or {}
        self.assertIn("--lifespan", cmd)
        self.assertEqual(cmd[cmd.index("--lifespan") + 1], "on")
        self.assertEqual(env.get("CLEANROOM_SKIP_STARTUP_AI"), "1")
        self.assertEqual(env.get("RADIOTEDU_WEBSOCKET_ENABLED"), "0")

    def test_guard_finds_packaged_backend_by_reserved_listener_port(self):
        original_platform = playout_guard.sys.platform
        original_check_output = playout_guard.subprocess.check_output
        calls = []

        def fake_check_output(cmd, **kwargs):
            calls.append((list(cmd), dict(kwargs)))
            return "4321\n"

        try:
            playout_guard.sys.platform = "win32"
            playout_guard.subprocess.check_output = fake_check_output
            self.assertEqual(playout_guard._backend_process_ids(), [4321])
        finally:
            playout_guard.sys.platform = original_platform
            playout_guard.subprocess.check_output = original_check_output

        command = calls[0][0][-1]
        self.assertIn("Get-NetTCPConnection -LocalPort 8100", command)
        self.assertIn("OwningProcess", command)
        self.assertIn("pythonw.exe", command)

    def test_guard_consumes_supervised_reload_and_replaces_backend(self):
        original_consume = playout_guard.consume_due_reload_request
        original_stop = playout_guard.stop_backend_processes
        original_start = playout_guard.start_backend
        original_log = playout_guard.log
        calls = []
        state = {
            "last_backend_start": 0.0,
            "backend_starting_since": None,
            "backend_bad_since": 1.0,
            "backend_last_status": "healthy",
            "backend_pid": 100,
        }
        try:
            playout_guard.consume_due_reload_request = lambda token: {
                "request_id": "reload-1",
                "backend_instance_id": "old-instance",
            }
            playout_guard.stop_backend_processes = lambda: calls.append("stop")
            playout_guard.start_backend = lambda: calls.append("start") or 4321
            playout_guard.log = lambda message: calls.append(str(message))

            replaced = playout_guard.process_supervised_reload_request(
                state,
                "supervisor-token",
            )
        finally:
            playout_guard.consume_due_reload_request = original_consume
            playout_guard.stop_backend_processes = original_stop
            playout_guard.start_backend = original_start
            playout_guard.log = original_log

        self.assertTrue(replaced)
        self.assertEqual(calls[0], "backend-reload accepted request_id=reload-1 previous_instance=old-instance")
        self.assertEqual(calls[1:3], ["stop", "start"])
        self.assertEqual(state["backend_pid"], 4321)
        self.assertEqual(state["backend_last_status"], "reloading")

    def test_frontend_validates_session_before_loading_operator_state(self):
        app_js = self._read_app_js()
        boot = self._extract_js_function(app_js, "boot")
        ensure_signed_in = self._extract_js_function(app_js, "ensureSignedIn")
        show_app = self._extract_js_function(app_js, "showApp")

        self.assertLess(boot.index("await ensureSignedIn()"), boot.index("await showApp()"))
        self.assertIn("await api('/api/auth/me')", ensure_signed_in)
        self.assertIn("clearSession()", ensure_signed_in)
        self.assertLess(show_app.index("await loadStations()"), show_app.index("await refreshAll(true)"))
        self.assertIn("startIdleTimer()", show_app)

    def test_frontend_api_wrapper_authenticates_refreshes_and_parses_payloads(self):
        app_js = self._read_app_js()
        api_wrapper = self._extract_js_function(app_js, "api")

        self.assertIn("headers.set('Authorization', `Bearer ${token}`)", api_wrapper)
        self.assertIn("response.status === 401", api_wrapper)
        self.assertIn("await refreshSession()", api_wrapper)
        self.assertIn("return JSON.parse(text)", api_wrapper)
        self.assertIn("parseResponseError", api_wrapper)

    def test_show_go_live_counts_visible_program_queue(self):
        shows_py = source_file("app", "api", "shows.py").read_text(
            encoding="utf-8"
        )
        start = shows_py.index("def go_live(")
        end = shows_py.index("\n\n@router.post", start + 1)
        go_live = shows_py[start:end]

        self.assertIn("ProgramQueueRepository(conn).list_items(body.station_id)", go_live)
        self.assertNotIn("FROM queue_items", go_live)

    def test_import_cleanup_paths_use_real_processing_summary(self):
        legacy_py = source_file("app", "api", "legacy.py").read_text(
            encoding="utf-8"
        )
        ranges = {
            "ytdlp": ("def _simulate_ytdlp_import(", "def _run_ytdlp_job_inline("),
            "scanner": ("def legacy_scanner_scan(", "def legacy_scanner_cleanup("),
            "upload": ("def legacy_upload_import(", "def legacy_media_stream("),
        }
        for name, (start_marker, end_marker) in ranges.items():
            with self.subTest(name=name):
                start = legacy_py.index(start_marker)
                end = legacy_py.index(end_marker, start)
                body = legacy_py[start:end]
                self.assertIn("_run_import_processing(", body)
                self.assertIn('_accumulate_import_processing(processing_summary, processing_result)', body)
                self.assertIn('"trim": processing_summary["trim"]', body)
                self.assertIn('"intro_clean": processing_summary["intro_clean"]', body)
                self.assertNotIn('"trim": {"trimmed": 0, "removed_seconds_total": 0.0}', body)
                self.assertNotIn('"intro_clean": {"cleaned": 0, "removed_seconds_total": 0.0}', body)

    def test_frontend_managed_library_sync_uses_entered_station_folder(self):
        app_js = self._read_app_js()
        body = self._extract_js_function(app_js, "syncLibraryFolder")

        self.assertIn("$('libraryFolder').value.trim()", body)
        self.assertIn("station_id: state.stationId", body)
        self.assertIn("recursive: $('libraryRecursive').checked", body)
        self.assertIn("api('/api/library/folder/sync'", body)
        self.assertIn("if (!result?.verified)", body)

    def test_import_processing_summary_accumulates_trim_and_intro_results(self):
        summary = legacy_api._empty_import_processing_summary()

        legacy_api._accumulate_import_processing(
            summary,
            {
                "trim": {"trimmed": True, "removed_seconds": 1.25},
                "intro_clean": {"cleaned": True, "removed_seconds": 2.5},
            },
        )
        legacy_api._accumulate_import_processing(
            summary,
            {
                "trim": {"trimmed": False, "error": "trim failed"},
                "intro_clean": {"cleaned": False, "error": "intro failed"},
            },
        )

        self.assertEqual(summary["trim"]["trimmed"], 1)
        self.assertEqual(summary["trim"]["failed"], 1)
        self.assertAlmostEqual(summary["trim"]["removed_seconds_total"], 1.25)
        self.assertEqual(summary["intro_clean"]["cleaned"], 1)
        self.assertEqual(summary["intro_clean"]["failed"], 1)
        self.assertAlmostEqual(summary["intro_clean"]["removed_seconds_total"], 2.5)

    def test_streaming_metadata_uses_mount_credentials_and_encodes_song(self):
        calls = []

        class FakeConnection:
            closed = False

            def close(self):
                self.closed = True

        class FakeSettingsRepository:
            def __init__(self, conn):
                self.conn = conn

            def get_system(self):
                return {"rocket_admin_host": "unused", "rocket_admin_port": "1"}

        conn = FakeConnection()
        original_init_db = streaming_api.init_db
        original_get_connection = streaming_api.get_connection
        original_settings_repo = streaming_api.SettingsRepository
        original_mount_credentials = streaming_api._mount_credentials
        original_request_text = streaming_api._request_text
        try:
            streaming_api.init_db = lambda: None
            streaming_api.get_connection = lambda: conn
            streaming_api.SettingsRepository = FakeSettingsRepository
            streaming_api._mount_credentials = lambda _conn, _settings, station_id, mount: {
                "host": "127.0.0.1",
                "port": 18123,
                "user": "source",
                "password": "secret",
            }

            def fake_request_text(url, user, password, data=None):
                calls.append({"url": url, "user": user, "password": password, "data": data})
                return {"ok": True, "status": 200, "body": "OK"}

            streaming_api._request_text = fake_request_text
            result = streaming_api.update_stream_metadata(
                streaming_api.MetadataUpdatePayload(
                    station_id=7,
                    mount="meta",
                    song="Codex Artist - Codex Title",
                ),
                _user={"id": 1},
            )
        finally:
            streaming_api.init_db = original_init_db
            streaming_api.get_connection = original_get_connection
            streaming_api.SettingsRepository = original_settings_repo
            streaming_api._mount_credentials = original_mount_credentials
            streaming_api._request_text = original_request_text

        self.assertTrue(conn.closed)
        self.assertEqual(result, {"ok": True, "status": 200, "body": "OK"})
        self.assertEqual(calls[0]["user"], "source")
        self.assertEqual(calls[0]["password"], "secret")
        self.assertIn("mount=%2Fmeta", calls[0]["url"])
        self.assertIn("song=Codex+Artist+-+Codex+Title", calls[0]["url"])

    def test_station_settings_offer_only_the_approved_aac_profiles(self):
        index_html = source_file("app", "static", "onair", "index.html").read_text(
            encoding="utf-8"
        )
        app_js = self._read_app_js()
        legacy_py = source_file("app", "api", "legacy.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('value="opus_64"', index_html)
        self.assertNotIn('value="opus_96"', index_html)
        self.assertNotIn('value="opus_192"', index_html)
        self.assertNotIn('value="opus_128"', index_html)
        self.assertNotIn('value="mp3_128"', index_html)
        self.assertNotIn('value="aac_lc_196"', index_html)
        self.assertIn('value="aac_low_192"', index_html)
        self.assertIn('value="aac_he_v2_64"', index_html)
        output_payload = self._extract_js_function(app_js, "currentOutputPayload")
        self.assertIn("currentIcecastProfile", output_payload)
        self.assertIn("stream_codec_profile: profile", output_payload)
        self.assertIn("streamProfileBitrate(profile)", output_payload)
        sync_start = legacy_py.index("def _sync_station_output_from_settings(")
        sync_end = legacy_py.index("\n\ndef _queue_set_playing", sync_start)
        sync_body = legacy_py[sync_start:sync_end]
        self.assertIn("resolve_stream_profile(", sync_body)
        self.assertIn('payload["stream_codec_profile"]', sync_body)

    def test_ai_settings_load_survives_bad_saved_numbers(self):
        import asyncio

        class FakeConnection:
            closed = False

            def close(self):
                self.closed = True

        class FakeSettingsRepository:
            def __init__(self, conn):
                self.conn = conn

            def get_station(self, station_id):
                self.station_id = station_id
                return {
                    "ai_host_enabled": "yes",
                    "ai_announcement_max_seconds": "not-a-number",
                    "ai_station_id_interval": "-5",
                    "ai_include_music_history": "no",
                    "ai_educational_segments": "on",
                }

        conn = FakeConnection()
        original_init_db = ai_host_api.init_db
        original_get_connection = ai_host_api.get_connection
        original_settings_repo = ai_host_api.SettingsRepository
        try:
            ai_host_api.init_db = lambda: None
            ai_host_api.get_connection = lambda: conn
            ai_host_api.SettingsRepository = FakeSettingsRepository
            result = asyncio.run(
                ai_host_api.get_ai_settings(
                    type("FakeRequest", (), {"query_params": {"station_id": "2"}})()
                )
            )
        finally:
            ai_host_api.init_db = original_init_db
            ai_host_api.get_connection = original_get_connection
            ai_host_api.SettingsRepository = original_settings_repo

        self.assertTrue(conn.closed)
        self.assertEqual(result["station_id"], 2)
        self.assertTrue(result["ai_host_enabled"])
        self.assertEqual(result["announcement_max_seconds"], 15)
        self.assertEqual(result["station_id_announcement_interval"], 60)
        self.assertFalse(result["include_music_history"])
        self.assertTrue(result["educational_segments_enabled"])

    def test_ai_settings_save_clamps_operator_numbers(self):
        import asyncio

        calls = []

        class FakeConnection:
            closed = False

            def close(self):
                self.closed = True

        class FakeSettingsRepository:
            def __init__(self, conn):
                self.conn = conn

            def upsert_station(self, station_id, settings_map):
                calls.append((station_id, dict(settings_map)))

        conn = FakeConnection()
        original_init_db = ai_host_api.init_db
        original_get_connection = ai_host_api.get_connection
        original_settings_repo = ai_host_api.SettingsRepository
        try:
            ai_host_api.init_db = lambda: None
            ai_host_api.get_connection = lambda: conn
            ai_host_api.SettingsRepository = FakeSettingsRepository
            result = asyncio.run(
                ai_host_api.update_ai_settings(
                    ai_host_api.AISettingsPayload(
                        station_id=3,
                        ai_host_enabled=True,
                        announcement_max_seconds=5000,
                        station_id_announcement_interval=1,
                    )
                )
            )
        finally:
            ai_host_api.init_db = original_init_db
            ai_host_api.get_connection = original_get_connection
            ai_host_api.SettingsRepository = original_settings_repo

        self.assertTrue(conn.closed)
        self.assertEqual(result, {"status": "ok", "station_id": 3})
        self.assertEqual(calls[0][0], 3)
        self.assertEqual(calls[0][1]["ai_announcement_max_seconds"], "120")
        self.assertEqual(calls[0][1]["ai_station_id_interval"], "60")
        self.assertEqual(calls[0][1]["ai_host_enabled"], "true")

    def test_ai_clear_cache_marker_hides_entries_immediately(self):
        original_cache_dir = ai_host_module.CACHE_DIR
        original_marker = ai_host_module.CACHE_CLEAR_MARKER
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            ai_host_module.CACHE_DIR = cache_dir
            ai_host_module.CACHE_CLEAR_MARKER = cache_dir / ".cleared_at"
            try:
                metadata_path = cache_dir / "announcement_demo.json"
                audio_path = cache_dir / "announcement_demo.wav"
                audio_path.write_bytes(b"RIFFdemo")
                metadata_path.write_text(
                    json.dumps(
                        {
                            "station_id": 1,
                            "title": "Demo",
                            "audio_path": str(audio_path),
                        }
                    ),
                    encoding="utf-8",
                )

                service = ai_host_module.AIHostService()
                self.assertEqual(service.count_cached_announcements(scan_limit=100), 1)

                result = service.clear_cache()

                self.assertTrue(result["background_delete"])
                self.assertEqual(service.count_cached_announcements(scan_limit=100), 0)
                self.assertEqual(
                    service.list_cached_announcements(
                        limit=10,
                        scan_limit=100,
                        verify_audio_files=False,
                    ),
                    [],
                )
            finally:
                ai_host_module.CACHE_DIR = original_cache_dir
                ai_host_module.CACHE_CLEAR_MARKER = original_marker

    def test_schedule_repository_preserves_event_name(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT DEFAULT '', artist TEXT DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE schedule_items ("
            "id INTEGER PRIMARY KEY, station_id INTEGER NOT NULL, track_id INTEGER NOT NULL, "
            "play_at TEXT NOT NULL, window_end TEXT, event_name TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'pending')"
        )
        conn.execute("INSERT INTO tracks (id, title, artist) VALUES (1, 'Fallback Playlist', '')")
        repo = ScheduleRepository(conn)

        item_id = repo.enqueue(
            station_id=1,
            track_id=1,
            play_at="2026-06-21T10:00:00Z",
            window_end="2026-06-21T10:30:00Z",
            event_name="Morning Interview",
        )

        row = repo.list_all(station_id=1)[0]
        self.assertEqual(int(row["id"]), item_id)
        self.assertEqual(row["event_name"], "Morning Interview")

    def test_track_type_sanitizer_preserves_ad_tracks(self):
        self.assertEqual(tracks_api._sanitize_track_type("ad"), "ad")
        self.assertEqual(tracks_api._sanitize_track_type("ads"), "ad")

    def test_repeated_ad_mismatch_is_suppressed_after_one_restart(self):
        playing = {
            "id": 20,
            "track_id": 201,
            "started_at": started_seconds_ago(3),
            "duration": 30.0,
            "track_type": "ad",
        }
        worker = make_worker(
            {
                "running": True,
                "program_running": True,
                "active_input_uri": "test://old-tts",
                "branch_health": {"icecast": True},
                "required_outputs": {"icecast": True},
            }
        )
        worker.ad_repo = FakeAdRepo(playing=playing)
        worker._track_runtime_fields = lambda _track_id: (
            "test://ad",
            "Ad",
            "",
            "ad",
        )

        self.assertTrue(worker._restart_playing_ad_item_if_runtime_mismatched(playing))
        worker.runtime_registry._status["active_input_uri"] = "test://old-tts"
        self.assertTrue(worker._restart_playing_ad_item_if_runtime_mismatched(playing))
        self.assertEqual(len(worker.runtime_registry.starts), 1)
        self.assertEqual(worker.ad_repo.failed, [20])

    def test_stale_runtime_generation_cannot_write_pcm(self):
        runtime = StationRuntime(process_factory=lambda _cmd, **_kwargs: None, station_id=1)
        stale_generation = runtime._current_playout_generation()
        runtime._next_playout_generation()
        sink = FakeSink()

        runtime._write_pcm_chunk_to_targets(
            b"abcd",
            [("local", sink)],
            generation=stale_generation,
        )

        self.assertEqual(sink.stdin.writes, [])

    def test_websocket_mic_session_decodes_media_recorder_webm_chunks(self):
        import subprocess
        import tempfile
        import time

        session = MicSession(station_id=999)
        ffmpeg_bin = str(session.ffmpeg_bin or "")
        if not ffmpeg_bin:
            raise unittest.SkipTest("ffmpeg unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            webm_path = Path(temp_dir) / "mic.webm"
            try:
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=1000:duration=1:sample_rate=48000",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "64k",
                        "-f",
                        "webm",
                        str(webm_path),
                    ],
                    check=True,
                )
            except Exception as exc:
                raise unittest.SkipTest(f"ffmpeg webm/opus generation unavailable: {exc}") from exc

            data = webm_path.read_bytes()
            session.start()
            try:
                for offset in range(0, len(data), 512):
                    session.push_chunk(data[offset : offset + 512])
                    time.sleep(0.002)
                deadline = time.monotonic() + 2.0
                pcm = b""
                while time.monotonic() < deadline:
                    pcm = session.read_pcm(48000)
                    samples = memoryview(pcm).cast("h")
                    peak = max(abs(int(sample)) for sample in samples) if samples else 0
                    if peak > 0:
                        break
                    time.sleep(0.05)
            finally:
                snapshot = session.snapshot()
                session.stop()

        self.assertTrue(snapshot["receiving"])
        self.assertGreater(len(pcm), 0)
        self.assertGreater(peak, 0)

    def test_silence_floor_survives_playout_generation_change(self):
        runtime = StationRuntime(process_factory=lambda _cmd, **_kwargs: None, station_id=1)
        sink = FakeSink()
        runtime._silence_floor_targets = lambda: [("icecast", sink)]
        try:
            runtime._start_silence_floor_worker()
            runtime._last_program_pcm_monotonic = 0.0
            runtime._next_playout_generation()
            import time

            time.sleep(0.12)
        finally:
            runtime._stop_silence_floor_worker()

        self.assertGreater(len(sink.stdin.writes), 0)
        self.assertLess(time.monotonic() - runtime._last_program_pcm_monotonic, 0.2)

    def test_silence_floor_does_not_inject_immediately_after_start(self):
        runtime = StationRuntime(process_factory=lambda _cmd, **_kwargs: None, station_id=1)
        sink = FakeSink()
        runtime._silence_floor_targets = lambda: [("icecast", sink)]
        try:
            runtime._start_silence_floor_worker()
            import time

            time.sleep(0.12)
        finally:
            runtime._stop_silence_floor_worker()

        self.assertEqual(len(sink.stdin.writes), 0)

    def test_required_outputs_healthy_accepts_floor_fed_connection(self):
        registry = StationRuntimeRegistry(runtime_factory=lambda _station_id: None)
        registry._runtimes[1] = FakeFloorRuntime(
            {
                "running": True,
                "program_running": False,
                "output_feed_active": True,
                "branch_health": {"icecast": True},
            }
        )
        registry._required_outputs[1] = {"icecast": True}

        self.assertTrue(registry.required_outputs_healthy(1))

    def test_icecast_sink_preserves_running_reconnecting_source_client(self):
        spawned = []

        def spawn(_cmd, **_kwargs):
            proc = FakeProc()
            spawned.append(proc)
            return proc

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/jazz",
            icecast_user="source",
            icecast_password="hackme",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink("ffmpeg", spawn, source_factory=FakeIcecastSource)
        stale_uploader = FakeProc()
        stale_encoder = FakeProc()
        sink._process = stale_uploader
        sink._encoder_process = stale_encoder
        sink._signature = sink._cfg_signature(cfg)
        sink.has_established_connection = lambda: False
        sink._cleanup_stale_source_clients = lambda _cfg: False
        sink._kick_remote_source = lambda _cfg: False

        sink.ensure_started(cfg)

        self.assertFalse(stale_uploader.terminated)
        self.assertFalse(stale_encoder.terminated)
        self.assertEqual(len(spawned), 0)
        self.assertIs(sink._encoder_process, stale_encoder)
        self.assertIs(sink._process, stale_uploader)

    def test_icecast_sink_drains_ffmpeg_stderr_pipe(self):
        spawned = []
        commands = []

        def spawn(command, **kwargs):
            commands.append(command)
            spawned.append(kwargs)
            return FakeProc()

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )

        IcecastAudioSink(
            "ffmpeg", spawn, source_factory=FakeIcecastSource
        ).ensure_started(cfg)

        self.assertEqual(len(spawned), 1)
        self.assertIs(spawned[0]["stderr"], subprocess.PIPE)
        rendered = " ".join(commands[0])
        self.assertNotIn("secret", rendered)
        self.assertNotIn("127.0.0.1", rendered)
        self.assertNotIn("icecast://", rendered)

    def test_icecast_sink_redacts_bounded_encoder_error(self):
        import io
        import time

        def spawn(_cmd, **_kwargs):
            proc = FakeProc()
            proc.stderr = io.BytesIO(
                b"av_interleaved_write_frame(): Broken pipe at "
                b"icecast://source:secret@127.0.0.1:8000/lofi\n"
            )
            return proc

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink("ffmpeg", spawn, source_factory=FakeIcecastSource)
        try:
            sink.ensure_started(cfg)
            deadline = time.monotonic() + 0.5
            while not sink.health_snapshot()["last_encoder_error"] and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot = sink.health_snapshot()
            self.assertIn("Broken pipe", snapshot["last_encoder_error"])
            self.assertNotIn("secret", snapshot["last_encoder_error"])
            self.assertIn("<redacted>", snapshot["last_encoder_error"])
            self.assertEqual(snapshot["encoder_error_count"], 1)
        finally:
            sink.stop()

    def test_icecast_sink_records_missing_mount_without_restarting_live_encoder(self):
        proc = FakeProc()
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink(
            "ffmpeg",
            lambda _cmd, **_kwargs: proc,
            mount_probe=lambda _cfg: False,
            probe_interval_sec=0.01,
            probe_warmup_sec=0.0,
            probe_failure_threshold=2,
            source_factory=FakeIcecastSource,
        )
        try:
            sink.ensure_started(cfg)
            import time

            deadline = time.monotonic() + 0.5
            while sink.health_snapshot()["mount_healthy"] is not False and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertTrue(sink.is_running())
            self.assertIs(sink.health_snapshot()["mount_healthy"], False)
            self.assertGreaterEqual(
                sink.health_snapshot()["consecutive_probe_failures"], 2
            )
        finally:
            sink.stop()

    def test_icecast_sink_keeps_accepting_pcm_while_mount_recovers(self):
        import time

        class WritablePipe:
            def __init__(self):
                self.writes = []

            def write(self, chunk):
                self.writes.append(bytes(chunk))
                return len(chunk)

            def flush(self):
                return None

            def close(self):
                return None

        proc = FakeProc()
        proc.stdin = WritablePipe()
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink(
            "ffmpeg",
            lambda _cmd, **_kwargs: proc,
            mount_probe=lambda _cfg: False,
            probe_interval_sec=0.01,
            probe_warmup_sec=0.0,
            probe_failure_threshold=1,
            source_factory=FakeIcecastSource,
        )
        try:
            sink.ensure_started(cfg)
            deadline = time.monotonic() + 0.5
            while sink.health_snapshot()["mount_healthy"] is not False and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(sink.is_running())
            self.assertTrue(sink.accepts_input())
            self.assertTrue(sink.write_pcm(b"pcm"))
            deadline = time.monotonic() + 0.5
            while not proc.stdin.writes and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(proc.stdin.writes), 1)
            self.assertEqual(proc.stdin.writes[0], b"pcm")
        finally:
            sink.stop()

    def test_icecast_sink_generates_clock_paced_continuity_pcm(self):
        import time

        class WritablePipe:
            def __init__(self):
                self.writes = []

            def write(self, chunk):
                self.writes.append((time.monotonic(), bytes(chunk)))
                return len(chunk)

            def flush(self):
                return None

            def close(self):
                return None

        class FlowingEncoderPipe:
            def __init__(self):
                self.closed = False

            def read(self, size):
                time.sleep(0.01)
                return b"encoded" if not self.closed else b""

            def close(self):
                self.closed = True

        proc = FakeProc()
        proc.stdin = WritablePipe()
        proc.stdout = FlowingEncoderPipe()
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink(
            "ffmpeg",
            lambda _cmd, **_kwargs: proc,
            source_factory=FakeIcecastSource,
        )
        try:
            sink.ensure_started(cfg)
            deadline = time.monotonic() + 1.0
            while len(proc.stdin.writes) < 8 and time.monotonic() < deadline:
                time.sleep(0.005)
            writes = list(proc.stdin.writes)
            self.assertGreaterEqual(len(writes), 8)
            elapsed = writes[-1][0] - writes[0][0]
            represented = sum(len(chunk) for _at, chunk in writes[:-1]) / (48000 * 2 * 2)
            # Windows service hosts can schedule Python worker threads in
            # coarse (~15 ms) quanta.  The filler must remain close to real
            # time and, critically, much faster than the former 50 ms/21 ms
            # under-feed that caused encoder starvation.
            self.assertLess(abs(elapsed - represented), 0.1)
            snapshot = sink.health_snapshot()
            self.assertGreaterEqual(snapshot["continuity_silence_chunks"], 8)
        finally:
            sink.stop()

    def test_icecast_sink_preserves_queued_programme_during_reconnect(self):
        sink = IcecastAudioSink(
            "ffmpeg",
            lambda _cmd, **_kwargs: FakeProc(),
            source_factory=FakeIcecastSource,
        )
        sink._connector_thread = type(
            "AliveConnector", (), {"is_alive": lambda self: True}
        )()
        sink._writer_stop.clear()
        self.assertTrue(sink.write_pcm(b"programme"))
        self.assertEqual(sink._pcm_queue.qsize(), 1)
        sink._close_encoder_connection()
        self.assertEqual(sink._pcm_queue.qsize(), 1)
        self.assertEqual(sink._pcm_queue.get_nowait(), b"programme")

    def test_icecast_sink_keeps_one_source_during_short_probe_failure_then_recovers(self):
        import threading
        import time

        spawned = []
        calls = {"probe": 0}
        allow_success = threading.Event()

        def spawn(_cmd, **_kwargs):
            proc = FakeProc()
            spawned.append(proc)
            return proc

        def probe(_cfg):
            calls["probe"] += 1
            if calls["probe"] <= 2:
                return False
            allow_success.wait(0.5)
            return allow_success.is_set()

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink(
            "ffmpeg",
            spawn,
            mount_probe=probe,
            probe_interval_sec=0.01,
            probe_warmup_sec=0.0,
            probe_failure_threshold=2,
            source_factory=FakeIcecastSource,
        )
        try:
            sink.ensure_started(cfg)
            deadline = time.monotonic() + 0.5
            while sink.health_snapshot()["mount_healthy"] is not False and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(sink.is_running())
            failures_before = sink.health_snapshot()["consecutive_probe_failures"]
            self.assertGreaterEqual(failures_before, 2)

            sink.ensure_started(cfg)
            recovery_snapshot = sink.health_snapshot()
            self.assertIs(recovery_snapshot["mount_healthy"], False)
            self.assertGreaterEqual(
                recovery_snapshot["consecutive_probe_failures"],
                failures_before,
            )
            self.assertEqual(len(spawned), 1)

            allow_success.set()
            deadline = time.monotonic() + 0.5
            while sink.health_snapshot()["mount_healthy"] is not True and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(sink.is_running())
            self.assertEqual(
                sink.health_snapshot()["consecutive_probe_failures"],
                0,
            )
        finally:
            allow_success.set()
            sink.stop()

    def test_icecast_sink_does_not_reconnect_for_listener_probe_failure(self):
        import time

        spawned = []

        def spawn(_cmd, **_kwargs):
            proc = FakeProc()
            spawned.append(proc)
            return proc

        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        sink = IcecastAudioSink(
            "ffmpeg",
            spawn,
            mount_probe=lambda _cfg: False,
            probe_interval_sec=0.01,
            probe_warmup_sec=0.0,
            probe_failure_threshold=1,
            reconnect_failure_threshold=3,
            source_factory=FakeIcecastSource,
        )
        try:
            first = sink.ensure_started(cfg)
            deadline = time.monotonic() + 0.5
            while sink.health_snapshot()["consecutive_probe_failures"] < 3 and time.monotonic() < deadline:
                time.sleep(0.01)

            second = sink.ensure_started(cfg)

            self.assertIs(first, second)
            self.assertFalse(first.terminated)
            self.assertEqual(len(spawned), 1)
            self.assertGreaterEqual(
                sink.health_snapshot()["consecutive_probe_failures"], 3
            )
        finally:
            sink.stop()

    def test_output_recovery_forwards_resume_offset_to_steady_state(self):
        runtime = StationRuntime(process_factory=lambda _cmd, **_kwargs: None, station_id=1)
        cfg = StationPipelineConfig(
            input_uri="test://song",
            icecast_host="127.0.0.1",
            icecast_port=8000,
            icecast_mount="/lofi",
            icecast_user="source",
            icecast_password="secret",
            local_output_enabled=False,
            output_device_id="",
        )
        observed = []
        runtime._stop_producers = lambda: None
        runtime._release_disabled_sinks = lambda _cfg: None
        runtime._should_use_live_mix = lambda: False
        runtime._launch_steady_state = (
            lambda _cfg, _signature, *, start_offset_seconds=0.0: observed.append(
                float(start_offset_seconds)
            )
        )

        runtime._restart_with(cfg, start_offset_seconds=12.5)

        self.assertEqual(observed, [12.5])

    def test_supervisor_does_not_stop_icecast_on_degraded_health(self):
        registry = FakeSupervisorRegistry(
            {
                "running": True,
                "program_running": True,
                "output_feed_active": True,
                "branch_health": {"icecast": False},
                "required_outputs": {"icecast": True},
            }
        )
        report = RuntimeSupervisor(registry).evaluate_station(1)

        self.assertEqual(report["action"], "degrade")
        self.assertEqual(registry.stop_calls, [])

    def test_worker_loop_defers_running_icecast_degradation_to_sink_reconnect(self):
        class Supervisor:
            def evaluate_station(self, _station_id):
                return {
                    "station_id": 1,
                    "running": True,
                    "action": "restart_last_resort",
                    "status": "required icecast output degraded",
                }

        starts = []
        manager = StationWorkerLoopManager(
            runtime_registry=object(),
            runtime_supervisor=Supervisor(),
        )
        manager._loops[1] = {
            "last_supervisor_action": "none",
            "last_supervisor_error": "",
            "last_supervisor_status": "",
            "next_recovery_at": 0.0,
            "runtime_bad_since": None,
        }
        manager._start_emergency_fallback = lambda *_args, **_kwargs: starts.append(True)

        result = manager._recover_runtime_if_needed(1, "silence://continuous")

        self.assertIsNone(result)
        self.assertEqual(starts, [])
        self.assertEqual(
            manager._loops[1]["last_supervisor_status"],
            "required icecast output degraded",
        )
        self.assertIsNone(manager._loops[1]["runtime_bad_since"])

    def test_guard_does_not_directly_recover_content_when_loop_is_healthy(self):
        original_api_get = playout_guard.api_get
        original_ensure_loop = playout_guard.ensure_worker_loop
        original_required = playout_guard.required_outputs_healthy
        original_recover = playout_guard.recover_content_from_fallback
        original_autostart = playout_guard.station_autostart_enabled
        try:
            loop_payload = {
                "running": True,
                "ticks": 1,
                "runtime": {"running": True, "branch_health": {"icecast": True}},
            }
            playout_guard.api_get = lambda _path: dict(loop_payload)
            playout_guard.ensure_worker_loop = (
                lambda _station_id, _station_state, payload: dict(payload)
            )
            playout_guard.required_outputs_healthy = lambda _runtime: True
            playout_guard.station_autostart_enabled = lambda _station_id: True
            playout_guard.recover_content_from_fallback = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct recovery called"))
            )
            state = {
                "last_summary": "",
                "last_health_log": 0.0,
                "last_fallback_content_recovery": 0.0,
                "runtime_bad_since": None,
            }

            playout_guard.supervise_station(1, state)
        finally:
            playout_guard.api_get = original_api_get
            playout_guard.ensure_worker_loop = original_ensure_loop
            playout_guard.required_outputs_healthy = original_required
            playout_guard.recover_content_from_fallback = original_recover
            playout_guard.station_autostart_enabled = original_autostart

    def test_guard_does_not_restart_running_loop_for_icecast_degradation(self):
        original_api_get = playout_guard.api_get
        original_ensure_loop = playout_guard.ensure_worker_loop
        original_tick = playout_guard.tick_worker
        original_emergency = playout_guard.emergency_fallback
        original_autostart = playout_guard.station_autostart_enabled
        calls = []
        try:
            loop_payload = {
                "running": True,
                "ticks": 12,
                "runtime": {
                    "running": True,
                    "backend": "ffmpeg-tee",
                    "branch_health": {"icecast": False, "local": False},
                    "required_outputs": {"icecast": True, "local": False},
                },
            }
            playout_guard.api_get = lambda _path: dict(loop_payload)
            playout_guard.ensure_worker_loop = (
                lambda _station_id, _station_state, payload: dict(payload)
            )
            playout_guard.station_autostart_enabled = lambda _station_id: True
            playout_guard.tick_worker = lambda *_args, **_kwargs: calls.append("tick")
            playout_guard.emergency_fallback = (
                lambda *_args, **_kwargs: calls.append("emergency")
            )
            state = {
                "last_summary": "",
                "last_health_log": 0.0,
                "last_fallback_content_recovery": 0.0,
                "runtime_bad_since": playout_guard.time.monotonic() - 120.0,
            }

            playout_guard.supervise_station(1, state)
        finally:
            playout_guard.api_get = original_api_get
            playout_guard.ensure_worker_loop = original_ensure_loop
            playout_guard.tick_worker = original_tick
            playout_guard.emergency_fallback = original_emergency
            playout_guard.station_autostart_enabled = original_autostart

        self.assertEqual(calls, [])

    def test_guard_escalates_a_frozen_worker_tick(self):
        original_api_get = playout_guard.api_get
        original_ensure_loop = playout_guard.ensure_worker_loop
        original_autostart = playout_guard.station_autostart_enabled
        try:
            loop_payload = {
                "running": True,
                "stalled": True,
                "tick_in_progress": True,
                "tick_elapsed_seconds": 75.0,
                "ticks": 8,
                "runtime": {
                    "running": True,
                    "branch_health": {"icecast": True},
                    "required_outputs": {"icecast": True},
                },
            }
            playout_guard.api_get = lambda _path: dict(loop_payload)
            playout_guard.ensure_worker_loop = (
                lambda _station_id, _station_state, payload: dict(payload)
            )
            playout_guard.station_autostart_enabled = lambda _station_id: True

            with self.assertRaises(playout_guard.BackendRestartRequired) as raised:
                playout_guard.supervise_station(2, {})

            self.assertIn("station=2", str(raised.exception))
            self.assertIn("75.0", str(raised.exception))
        finally:
            playout_guard.api_get = original_api_get
            playout_guard.ensure_worker_loop = original_ensure_loop
            playout_guard.station_autostart_enabled = original_autostart

    def test_guard_rotates_its_log_before_it_can_grow_without_bound(self):
        original_path = playout_guard.LOG_PATH
        original_max = playout_guard.LOG_MAX_BYTES
        original_backups = playout_guard.LOG_BACKUP_COUNT
        try:
            with tempfile.TemporaryDirectory() as td:
                log_path = Path(td) / "playout_guard.log"
                log_path.write_text("x" * 32, encoding="utf-8")
                playout_guard.LOG_PATH = log_path
                playout_guard.LOG_MAX_BYTES = 16
                playout_guard.LOG_BACKUP_COUNT = 2

                playout_guard.log("after rotation")

                self.assertIn("after rotation", log_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    log_path.with_name("playout_guard.log.1").read_text(
                        encoding="utf-8"
                    ),
                    "x" * 32,
                )
        finally:
            playout_guard.LOG_PATH = original_path
            playout_guard.LOG_MAX_BYTES = original_max
            playout_guard.LOG_BACKUP_COUNT = original_backups

    def test_guard_never_restarts_operator_stopped_station(self):
        original_autostart = playout_guard.station_autostart_enabled
        original_api_get = playout_guard.api_get
        try:
            playout_guard.station_autostart_enabled = lambda _station_id: False
            playout_guard.api_get = lambda _path: (_ for _ in ()).throw(
                AssertionError("disabled station was touched")
            )
            state = {
                "loop_bad_since": 1.0,
                "runtime_bad_since": 1.0,
            }

            playout_guard.supervise_station(1, state)

            self.assertIsNone(state["loop_bad_since"])
            self.assertIsNone(state["runtime_bad_since"])
        finally:
            playout_guard.station_autostart_enabled = original_autostart
            playout_guard.api_get = original_api_get

    def test_ai_track_intro_prompt_uses_listener_friendly_title(self):
        service = AIHostService()
        friendly = service._listener_friendly_title("Mozart - Lacrimosa K929 in D minor")
        messages = service._llm_messages(
            spoken_template="Write a short natural intro.",
            station_name="RadioTEDU Spark",
            title="Mozart - Lacrimosa K929 in D minor",
            artist="Mozart",
            spoken_title=friendly,
            persona="smooth_evening",
            max_seconds=8,
            greeting="",
        )
        prompt_text = "\n".join(str(message["content"]) for message in messages)

        self.assertIn("Listener-friendly title hint: Mozart - Lacrimosa", prompt_text)
        self.assertIn("Do not say 'Up next is'", prompt_text)
        self.assertIn("Do not read the raw title verbatim", prompt_text)
        self.assertNotIn("Use the exact station name", prompt_text)

    def test_ai_status_uses_lightweight_cache_count(self):
        original_cache_dir = ai_host_module.CACHE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cache_dir = Path(tmp)
                ai_host_module.CACHE_DIR = cache_dir
                audio_path = cache_dir / "announcement.wav"
                audio_path.write_bytes(b"not-real-audio")
                for index in range(3):
                    metadata = {
                        "audio_path": str(audio_path),
                        "station_id": 7,
                        "title": f"Cached {index}",
                        "duration_seconds": 0,
                    }
                    (cache_dir / f"announcement_{index}.json").write_text(
                        json.dumps(metadata),
                        encoding="utf-8",
                    )

                service = AIHostService()

                def fail_duration_probe(_path: str, _fallback_text: str = "") -> float:
                    raise AssertionError("status should not probe cached audio duration")

                service._duration_seconds = fail_duration_probe  # type: ignore[method-assign]

                status = service.get_status(settings={"ai_tts_provider": "edge-tts"}, station_id=7)
                listed = service.list_cached_announcements(
                    station_id=7,
                    limit=2,
                    include_duration_fallback=False,
                    scan_limit=10,
                )
                scan_capped = service.list_cached_announcements(
                    station_id=7,
                    limit=3,
                    include_duration_fallback=False,
                    scan_limit=1,
                )

                self.assertEqual(status["cache_size"], 3)
                self.assertEqual(status["announcements_generated"], 3)
                self.assertEqual(len(listed), 2)
                self.assertEqual(len(scan_capped), 1)
                self.assertEqual(listed[0]["duration_seconds"], 0)
        finally:
            ai_host_module.CACHE_DIR = original_cache_dir


if __name__ == "__main__":
    unittest.main()
