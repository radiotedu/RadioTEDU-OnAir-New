from app.db import get_connection
from app.services.ai_host import reset_ai_host


def test_ai_settings_update_persists_station_values(client):
    payload = {
        "station_id": 1,
        "ai_host_enabled": True,
        "llm_model": "Qwen/test-model",
        "tts_provider": "windows-sapi",
        "tts_model_path": "C:/models/voice",
        "voice_persona": "evening",
        "announcement_max_seconds": 22,
        "include_music_history": False,
        "educational_segments_enabled": True,
        "station_id_announcement_interval": 2400,
        "prompt_template": "You're listening to {station_name}. Up next is {track_title}{artist_phrase}.",
    }

    response = client.post("/api/ai/settings", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"

    settings_response = client.get("/api/ai/settings", params={"station_id": 1})

    assert settings_response.status_code == 200, settings_response.text
    data = settings_response.json()
    assert data["station_id"] == 1
    assert data["ai_host_enabled"] is True
    assert data["llm_model"] == "Qwen/test-model"
    assert data["tts_provider"] == "windows-sapi"
    assert data["tts_model_path"] == "C:/models/voice"
    assert data["voice_persona"] == "evening"
    assert data["announcement_max_seconds"] == 22
    assert data["include_music_history"] is False
    assert data["educational_segments_enabled"] is True
    assert data["station_id_announcement_interval"] == 2400
    assert data["prompt_template"] == "You're listening to {station_name}. Up next is {track_title}{artist_phrase}."


def test_ai_status_uses_service_status(client, monkeypatch):
    class _FakeAIHost:
        def get_status(self, *, settings=None):
            return {
                "llm_loaded": True,
                "tts_loaded": True,
                "tts_model_exists": True,
                "current_persona": "night",
                "cache_size": 3,
                "announcements_generated": 3,
                "llm_provider": "local-qwen",
                "tts_provider": "local-qwen-tts",
                "ready": True,
                "prompt_template_configured": True,
            }

    reset_ai_host()
    monkeypatch.setattr("app.services.ai_host.get_ai_host", lambda: _FakeAIHost())
    client.post(
        "/api/ai/settings",
        json={
            "station_id": 1,
            "ai_host_enabled": True,
            "prompt_template": "Test {station_name}",
        },
    )

    response = client.get("/api/ai/status", params={"station_id": 1})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ai_host_enabled"] is True
    assert data["llm_loaded"] is True
    assert data["tts_loaded"] is True
    assert data["tts_model_exists"] is True
    assert data["current_persona"] == "night"
    assert data["cache_size"] == 3
    assert data["announcements_generated"] == 3
    assert data["llm_provider"] == "local-qwen"
    assert data["tts_provider"] == "local-qwen-tts"
    assert data["operational"] is True
    assert data["prompt_template_configured"] is True


def test_disabling_ai_purges_only_pending_generated_announcements(client):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO stations (id, name) VALUES (2, 'AI Disable Test')")
    cursor.execute(
        "INSERT INTO tracks (station_id, title, track_type, file_path, is_active) "
        "VALUES (2, 'Generated Intro', 'announcement', 'C:/ai/intro.wav', 1)"
    )
    announcement_id = int(cursor.lastrowid)
    cursor.execute(
        "INSERT INTO tracks (station_id, title, track_type, file_path, is_active) "
        "VALUES (2, 'Music', 'music', 'C:/music/song.mp3', 1)"
    )
    music_id = int(cursor.lastrowid)
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 8, 'pending')",
        (announcement_id,),
    )
    cursor.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) VALUES (2, ?, 9, 'pending')",
        (music_id,),
    )
    conn.commit()

    response = client.post(
        "/api/ai/settings",
        json={"station_id": 2, "ai_host_enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "station_id": 2}

    rows = conn.execute(
        "SELECT track_id, position, status FROM queue_items WHERE station_id=2 ORDER BY position, id"
    ).fetchall()
    assert [(int(row["track_id"]), int(row["position"]), str(row["status"])) for row in rows] == [
        (music_id, 1, "pending")
    ]
