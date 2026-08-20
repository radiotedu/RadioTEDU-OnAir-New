from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.soak_test_onair import Config, _json_safe, _runtime_projection, run


class _FixtureHandler(BaseHTTPRequestHandler):
    stream_bytes = b"\xff\xf1" + (b"audio" * 4096)

    def log_message(self, *_args):  # pragma: no cover - keep pytest output quiet
        return

    def _write_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/auth/login":
            self._write_json({"access_token": "fixture-token"})
        else:
            self._write_json({}, 404)

    def do_GET(self):
        if self.path == "/api/health":
            self._write_json({"status": "ok", "version": "1.0.2", "database": {"integrity": "ok"}})
        elif self.path == "/api/stations":
            self._write_json([{"id": 1, "name": "Fixture"}])
        elif self.path == "/api/stations/runtimes":
            self._write_json([{"state": "playing", "bytes_sent": 128, "reconnects": 0, "now_playing": "Fixture Track"}])
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "audio/aac")
            self.end_headers()
            self.wfile.write(self.stream_bytes)
        else:
            self._write_json({}, 404)


def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_projection_redacts_sensitive_fields():
    safe = _json_safe({"password": "test-" + "x" * 24, "state": "playing", "nested": {"token": "hidden"}})
    assert safe == {"state": "playing", "nested": {}}
    projection = _runtime_projection([{"state": "playing", "bytes_sent": 7, "reconnects": 2, "title": "Song"}])
    assert projection["states"] == ["playing"]
    assert projection["max_bytes"] == 7
    assert projection["max_reconnects"] == 2
    assert projection["track_present"] is True
    assert projection["runtime_healthy"] is True


def test_projection_reports_station_transport_recovery_and_encoder_errors():
    projection = _runtime_projection(
        {
            "running": True,
            "worker_loop": {"running": True},
            "branch_health": {"icecast": True, "local": False},
            "required_outputs": {"icecast": True, "local": False},
            "active_input_uri": r"H:\\RadioTEDU\\track.flac",
            "recovery": {"attempt_count": 4},
            "icecast_mount_health": {
                "encoder_error_count": 9,
                "last_encoder_error": "Error number -10054 occurred",
            },
        }
    )

    assert projection["runtime_healthy"] is True
    assert projection["recovery_attempt_count"] == 4
    assert projection["encoder_error_count"] == 9
    assert projection["encoder_error_present"] is True


def test_once_soak_records_authenticated_api_and_audio_without_credentials(tmp_path: Path):
    server, _thread = _server()
    try:
        port = server.server_address[1]
        password = "test-" + "x" * 24
        password_file = tmp_path / "initial-admin-password.txt"
        password_file.write_text(f"Username: admin\nPassword: {password}\n", encoding="utf-8")
        output = tmp_path / "evidence.jsonl"
        config = Config(
            api_base=f"http://127.0.0.1:{port}",
            username="admin",
            password_file=password_file,
            stream_url=f"http://127.0.0.1:{port}/stream",
            station_id=1,
            interval_seconds=1,
            duration_seconds=1,
            output=output,
            timeout_seconds=3,
            once=True,
        )
        assert run(config) == 0
        text = output.read_text(encoding="utf-8")
        assert "soak_summary" in text
        assert '"passed":true' in text
        assert password not in text
    finally:
        server.shutdown()
        server.server_close()
