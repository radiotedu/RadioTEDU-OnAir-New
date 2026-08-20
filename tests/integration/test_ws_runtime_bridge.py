import app.api.runtime as runtime_api

from tests.conftest import login_and_get_headers


class _FakeRegistry:
    def __init__(self):
        self.running = {}

    def start_station(self, station_id: int, input_uri: str, stream_title: str = "", stream_artist: str = "", track_type: str = "music", crossfade_seconds=None):
        self.running[int(station_id)] = True
        return self.status(station_id)

    def stop_station(self, station_id: int):
        self.running[int(station_id)] = False
        return self.status(station_id)

    def status(self, station_id: int):
        return {
            "station_id": int(station_id),
            "running": bool(self.running.get(int(station_id), False)),
            "backend": "fake",
            "transition_mode": "none",
            "branch_health": {"icecast": True, "local": True},
            "required_outputs": {"icecast": True, "local": True},
        }


class _FakeSupervisor:
    def evaluate_station(self, station_id: int):
        return {"station_id": int(station_id), "action": "none"}


class _FakeLoopManager:
    def status(self, station_id: int):
        return {
            "station_id": int(station_id),
            "running": False,
            "interval_sec": None,
            "ticks": 0,
            "last_result": None,
            "last_error": "",
        }


def test_queue_and_runtime_changes_reach_active_socket(client, monkeypatch):
    monkeypatch.setattr(runtime_api, "runtime_registry", _FakeRegistry())
    monkeypatch.setattr(runtime_api, "runtime_supervisor", _FakeSupervisor())
    monkeypatch.setattr(runtime_api, "worker_loop_manager", _FakeLoopManager())

    created = client.post(
        "/api/tracks",
        json={
            "station_id": 1,
            "title": "Socket Song",
            "artist": "Tester",
            "file_path": "C:/media/music/socket-song.mp3",
            "track_type": "music",
        },
    )
    assert created.status_code == 200
    track_id = int(created.json().get("id") or created.json().get("track_id"))

    token = login_and_get_headers(client, "admin", "changeme")["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/ws?token={token}&station_id=1") as ws:
        queued = client.post("/api/queue/push", json={"station_id": 1, "track_id": track_id})
        assert queued.status_code == 200
        queue_event = ws.receive_json()
        assert queue_event["type"] == "queue.updated"
        assert queue_event["station_id"] == 1

        started = client.post(
            "/api/runtime/1/operator-start-track",
            json={"input_uri": "C:/music/fallback.mp3"},
        )
        assert started.status_code == 200
        runtime_event = ws.receive_json()
        assert runtime_event["type"] in {"runtime.updated", "engine.event", "track.changed"}
        assert runtime_event["station_id"] == 1
