from app.api import integrations


def test_juke_library_routes_use_the_protected_config_and_confirm_actions(
    client, monkeypatch
):
    monkeypatch.setattr(
        integrations,
        "_juke_library_config_path",
        lambda: r"C:\protected\juke.env",
    )
    monkeypatch.setattr(
        integrations,
        "list_juke_library",
        lambda config_path, **kwargs: {
            "config_path_seen": config_path,
            "items": [],
            **kwargs,
        },
    )
    listed = client.get(
        "/api/integrations/radiotedu/juke-library",
        params={"query": "song", "root_id": "primary", "limit": 25},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["config_path_seen"] == r"C:\protected\juke.env"
    assert listed.json()["query"] == "song"

    denied = client.post(
        "/api/integrations/radiotedu/juke-library/retire",
        json={
            "root_id": "primary",
            "relative_path": "song.mp3",
            "confirmation": "",
        },
    )
    assert denied.status_code == 400

    monkeypatch.setattr(
        integrations,
        "retire_juke_library_item",
        lambda config_path, **kwargs: {
            "ok": True,
            "config_path_seen": config_path,
            **kwargs,
        },
    )
    retired = client.post(
        "/api/integrations/radiotedu/juke-library/retire",
        json={
            "root_id": "primary",
            "relative_path": "song.mp3",
            "confirmation": "RETIRE JUKE SONG",
        },
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["relative_path"] == "song.mp3"

    monkeypatch.setattr(
        integrations,
        "restore_juke_library_item",
        lambda config_path, **kwargs: {
            "ok": True,
            "config_path_seen": config_path,
            **kwargs,
        },
    )
    restored = client.post(
        "/api/integrations/radiotedu/juke-library/restore",
        json={
            "root_id": "primary",
            "trash_path": ".radiotedu-trash/stamp/song.mp3",
            "confirmation": "RESTORE JUKE SONG",
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["trash_path"].endswith("song.mp3")
