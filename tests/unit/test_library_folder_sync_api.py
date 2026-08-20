from pathlib import Path

from app.db import get_connection, init_db


def _fake_metadata(file_path: str, fallback_title: str = "Track", **_kwargs) -> dict:
    return {
        "title": Path(file_path).stem or fallback_title,
        "artist": "Test Artist",
        "duration": 180.0,
    }


def _audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3-test-audio")
    return path.resolve()


def test_folder_sync_persists_recursive_profile_without_http(tmp_path, monkeypatch):
    from app.api import legacy

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setattr(legacy, "_get_audio_metadata", _fake_metadata)
    managed = tmp_path / "official-live-folder"
    _audio(managed / "Incoming" / "Track.flac")
    init_db()
    conn = get_connection()
    try:
        result = legacy._sync_station_library_folder_with_connection(
            legacy.LibraryFolderSyncPayload(
                station_id=1,
                folder=str(managed),
                recursive=True,
                track_type="music",
                mode="merge",
                skip_unplayable=True,
            ),
            conn,
        )
        recursive = conn.execute(
            "SELECT value FROM station_settings "
            "WHERE station_id=1 AND key='library_recursive'"
        ).fetchone()
    finally:
        conn.close()

    assert result["verified"] is True
    assert recursive["value"] == "true"


def test_replace_sync_is_exact_idempotent_and_cleans_pending_queue(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)
    managed = tmp_path / "pop"
    first = _audio(managed / "First.mp3")
    second = _audio(managed / "nested" / "Second.wav")
    old = _audio(tmp_path / "old" / "Old Rock.mp3")

    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO stations (id, name) VALUES (2, 'Managed Test')")
    cursor = conn.execute(
        "INSERT INTO tracks "
        "(station_id, title, artist, track_type, file_path, is_active, duration) "
        "VALUES (2, 'Old Rock', '', 'music', ?, 1, 120)",
        (str(old),),
    )
    old_track_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO queue_items (station_id, track_id, position, status) "
        "VALUES (2, ?, 1, 'pending')",
        (old_track_id,),
    )
    conn.commit()
    conn.close()

    payload = {
        "station_id": 2,
        "folder": str(managed),
        "mode": "replace",
        "profile_label": "Pop",
        "default_genre": "Pop",
        "default_language": "en",
    }
    response = client.post("/api/library/folder/sync", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verified"] is True
    assert result["expected_files"] == 2
    assert result["active_files"] == 2
    assert result["added"] == 2
    assert result["deactivated"] == 1
    assert result["pending_queue_items_removed"] == 1

    conn = get_connection()
    active_rows = conn.execute(
        "SELECT file_path, genre, language FROM tracks "
        "WHERE station_id=2 AND track_type='music' AND is_active=1 ORDER BY file_path"
    ).fetchall()
    assert {Path(row["file_path"]).resolve() for row in active_rows} == {first, second}
    assert {str(row["genre"]) for row in active_rows} == {"Pop"}
    assert {str(row["language"]) for row in active_rows} == {"en"}
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM queue_items "
        "WHERE station_id=2 AND track_id=? AND status='pending'",
        (old_track_id,),
    ).fetchone()["c"] == 0
    settings = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            "SELECT key, value FROM station_settings WHERE station_id=2"
        ).fetchall()
    }
    conn.close()
    assert Path(settings["music_library_folder"]).resolve() == managed.resolve()
    assert settings["library_management_mode"] == "replace"
    assert settings["library_recursive"] == "true"
    assert settings["library_profile_label"] == "Pop"
    assert settings["library_active_files"] == "2"

    second_response = client.post("/api/library/folder/sync", json=payload)
    assert second_response.status_code == 200, second_response.text
    second_result = second_response.json()
    assert second_result["verified"] is True
    assert second_result["added"] == 0
    assert second_result["retained"] == 2
    assert second_result["deactivated"] == 0


def test_managed_folders_are_isolated_per_station(client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)
    pop_folder = tmp_path / "pop"
    rock_folder = tmp_path / "rock-en"
    pop_track = _audio(pop_folder / "Pop Song.mp3")
    rock_track = _audio(rock_folder / "Rock Song.mp3")

    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO stations (id, name) VALUES (2, 'Rock')")
    conn.commit()
    conn.close()

    pop_response = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(pop_folder),
            "mode": "replace",
            "profile_label": "Pop",
            "default_genre": "Pop",
        },
    )
    rock_response = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 2,
            "folder": str(rock_folder),
            "mode": "replace",
            "profile_label": "Rock (EN)",
            "default_genre": "Rock",
            "default_language": "en",
        },
    )
    assert pop_response.status_code == 200, pop_response.text
    assert rock_response.status_code == 200, rock_response.text

    conn = get_connection()
    rows = conn.execute(
        "SELECT station_id, file_path, genre, language FROM tracks "
        "WHERE is_active=1 AND track_type='music' ORDER BY station_id"
    ).fetchall()
    conn.close()
    assert [(int(row["station_id"]), Path(row["file_path"]).resolve()) for row in rows] == [
        (1, pop_track),
        (2, rock_track),
    ]
    assert str(rows[1]["genre"]) == "Rock"
    assert str(rows[1]["language"]) == "en"


def test_jingle_folder_profile_does_not_overwrite_music_profile(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)
    music_folder = tmp_path / "music"
    jingle_folder = tmp_path / "jingles"
    _audio(music_folder / "Song.mp3")
    _audio(jingle_folder / "Station ID.mp3")

    music_response = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(music_folder),
            "track_type": "music",
            "mode": "replace",
            "profile_label": "Pop",
            "default_genre": "Pop",
        },
    )
    jingle_response = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(jingle_folder),
            "track_type": "jingle",
            "mode": "replace",
            "profile_label": "Jingles",
        },
    )
    assert music_response.status_code == 200, music_response.text
    assert jingle_response.status_code == 200, jingle_response.text

    conn = get_connection()
    settings = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            "SELECT key, value FROM station_settings WHERE station_id=1"
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT track_type, COUNT(*) AS count FROM tracks "
        "WHERE station_id=1 AND is_active=1 GROUP BY track_type"
    ).fetchall()
    conn.close()

    assert settings["library_profile_label"] == "Pop"
    assert settings["library_management_mode"] == "replace"
    assert settings["library_active_files"] == "1"
    assert settings["jingle_library_profile_label"] == "Jingles"
    assert settings["jingle_library_management_mode"] == "replace"
    assert settings["jingle_library_active_files"] == "1"
    assert Path(settings["jingle_library_folder"]).resolve() == jingle_folder.resolve()
    assert {str(row["track_type"]): int(row["count"]) for row in rows} == {
        "jingle": 1,
        "music": 1,
    }


def test_folder_sync_rejects_entire_folder_before_writes_when_any_audio_is_unplayable(
    client, tmp_path, monkeypatch
):
    managed = tmp_path / "managed"
    good = _audio(managed / "Good.mp3")
    bad = _audio(managed / "Broken.mp3")

    def probe(file_path: str, **_kwargs):
        if Path(file_path).resolve() == bad:
            raise ValueError("decoder rejected file")
        return {"title": Path(file_path).stem, "artist": "", "duration": 120.0}

    monkeypatch.setattr("app.api.legacy._get_audio_metadata", probe)
    response = client.post(
        "/api/library/folder/sync",
        json={"station_id": 1, "folder": str(managed), "mode": "replace"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["invalid_count"] == 1
    assert detail["files"][0]["file"] == bad.name

    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM tracks WHERE station_id=1 AND file_path IN (?, ?)",
        (str(good), str(bad)),
    ).fetchone()["count"]
    conn.close()
    assert int(count) == 0


def test_folder_sync_can_skip_and_report_unplayable_audio_when_explicitly_requested(
    client, tmp_path, monkeypatch
):
    managed = tmp_path / "managed"
    good = _audio(managed / "Good.mp3")
    bad = _audio(managed / "Broken.mp3")

    def probe(file_path: str, **_kwargs):
        if Path(file_path).resolve() == bad:
            raise ValueError("decoder rejected file")
        return {"title": Path(file_path).stem, "artist": "", "duration": 120.0}

    monkeypatch.setattr("app.api.legacy._get_audio_metadata", probe)
    response = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(managed),
            "mode": "replace",
            "skip_unplayable": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["verified"] is True
    assert payload["expected_files"] == 1
    assert payload["active_files"] == 1
    assert payload["invalid_files_skipped"] == 1
    assert payload["invalid_files"][0]["file"] == bad.name

    conn = get_connection()
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE station_id=1 AND is_active=1"
    ).fetchall()
    conn.close()
    assert {Path(str(row["file_path"])).resolve() for row in rows} == {good}


def test_incremental_folder_sync_reuses_active_metadata_and_probes_only_new_files(
    client, tmp_path, monkeypatch
):
    managed = tmp_path / "large-managed-library"
    first = _audio(managed / "First.mp3")
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)

    initial = client.post(
        "/api/library/folder/sync",
        json={"station_id": 1, "folder": str(managed), "mode": "merge"},
    )
    assert initial.status_code == 200, initial.text

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("active verified media must not be reprobed")

    monkeypatch.setattr("app.api.legacy._get_audio_metadata", unexpected_probe)
    unchanged = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(managed),
            "mode": "merge",
            "incremental": True,
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["metadata_reused"] == 1
    assert unchanged.json()["metadata_probed"] == 0

    second = _audio(managed / "Second.flac")
    probed = []

    def record_probe(file_path: str, **kwargs):
        probed.append(Path(file_path).resolve())
        return _fake_metadata(file_path, **kwargs)

    monkeypatch.setattr("app.api.legacy._get_audio_metadata", record_probe)
    changed = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(managed),
            "mode": "merge",
            "incremental": True,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["metadata_reused"] == 1
    assert changed.json()["metadata_probed"] == 1
    assert probed == [second]
    conn = get_connection()
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE station_id=1 AND is_active=1"
    ).fetchall()
    conn.close()
    assert {first, second} == {
        Path(row["file_path"]).resolve()
        for row in rows
    }


def test_incremental_folder_sync_reprobes_metadata_when_file_changes(
    tmp_path, monkeypatch
):
    from app.api import legacy

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    managed = tmp_path / "metadata-live-folder"
    track = _audio(managed / "Mutable.flac")
    titles = iter(("Original title", "Updated title"))

    def probe(file_path: str, **_kwargs):
        return {
            "title": next(titles),
            "artist": "Live Folder Artist",
            "duration": 180.0,
        }

    monkeypatch.setattr(legacy, "_get_audio_metadata", probe)
    init_db()
    conn = get_connection()
    initial = legacy._sync_station_library_folder_with_connection(
        legacy.LibraryFolderSyncPayload(
            station_id=1,
            folder=str(managed),
            mode="replace",
        ),
        conn,
    )
    assert initial["verified"] is True

    track.write_bytes(track.read_bytes() + b"-metadata-rewrite")
    changed = legacy._sync_station_library_folder_with_connection(
        legacy.LibraryFolderSyncPayload(
            station_id=1,
            folder=str(managed),
            mode="replace",
            incremental=True,
        ),
        conn,
    )
    assert changed["metadata_reused"] == 0
    assert changed["metadata_probed"] == 1

    row = conn.execute(
        "SELECT title FROM tracks WHERE station_id=1 AND file_path=?",
        (str(track),),
    ).fetchone()
    conn.close()
    assert row["title"] == "Updated title"


def test_managed_folder_caches_sidecar_cover_art_in_public_media_root(
    tmp_path, monkeypatch
):
    from app.api import legacy

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    monkeypatch.setattr(legacy, "_get_audio_metadata", _fake_metadata)
    managed = tmp_path / "artwork-live-folder"
    track = _audio(managed / "Covered Song.flac")
    track.with_suffix(".jpg").write_bytes(b"test-jpeg-art")
    init_db()
    conn = get_connection()
    result = legacy._sync_station_library_folder_with_connection(
        legacy.LibraryFolderSyncPayload(
            station_id=1,
            folder=str(managed),
            mode="replace",
        ),
        conn,
    )
    row = conn.execute(
        "SELECT cover_art_url FROM tracks WHERE station_id=1 AND file_path=?",
        (str(track),),
    ).fetchone()
    conn.close()

    assert result["verified"] is True
    assert str(row["cover_art_url"]).startswith("1/cover-art/")
    cached = tmp_path / "media" / str(row["cover_art_url"])
    assert cached.read_bytes() == b"test-jpeg-art"


def test_managed_folder_reads_flac_picture_block_without_audio_decode(
    tmp_path, monkeypatch
):
    from app.api import legacy

    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    picture = b"direct-flac-picture"
    mime = b"image/png"
    block = (
        (3).to_bytes(4, "big")
        + len(mime).to_bytes(4, "big")
        + mime
        + (0).to_bytes(4, "big")
        + (1).to_bytes(4, "big") * 4
        + len(picture).to_bytes(4, "big")
        + picture
    )
    track = tmp_path / "Picture.flac"
    track.write_bytes(b"fLaC" + bytes([0x80 | 6]) + len(block).to_bytes(3, "big") + block)
    result = legacy._cache_managed_cover_art(
        track,
        1,
        {"has_embedded_art": True},
    )

    assert result.endswith(".png")
    assert (tmp_path / "media" / result).read_bytes() == picture


def test_guarded_watcher_sync_cannot_restore_stale_managed_folder(
    client, tmp_path, monkeypatch
):
    old_folder = tmp_path / "old-managed"
    new_folder = tmp_path / "new-managed"
    old_track = _audio(old_folder / "Old.mp3")
    new_track = _audio(new_folder / "New.mp3")
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)

    initial = client.post(
        "/api/library/folder/sync",
        json={"station_id": 1, "folder": str(old_folder), "mode": "replace"},
    )
    assert initial.status_code == 200, initial.text
    changed = client.post(
        "/api/library/folder/sync",
        json={"station_id": 1, "folder": str(new_folder), "mode": "merge"},
    )
    assert changed.status_code == 200, changed.text

    stale = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(old_folder),
            "mode": "replace",
            "incremental": True,
            "guard_configured_folder": True,
        },
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["skipped"] is True
    assert stale.json()["reason"] == "stale_managed_library_profile"

    conn = get_connection()
    configured = conn.execute(
        "SELECT value FROM station_settings WHERE station_id=1 AND key='music_library_folder'"
    ).fetchone()["value"]
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE station_id=1 AND is_active=1"
    ).fetchall()
    conn.close()
    assert Path(str(configured)).resolve() == new_folder.resolve()
    assert {Path(str(row["file_path"])).resolve() for row in rows} == {
        old_track,
        new_track,
    }


def test_guarded_watcher_rechecks_profile_after_slow_probe(client, tmp_path, monkeypatch):
    old_folder = tmp_path / "old-managed"
    new_folder = tmp_path / "new-managed"
    old_track = _audio(old_folder / "Old.mp3")
    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _fake_metadata)
    initial = client.post(
        "/api/library/folder/sync",
        json={"station_id": 1, "folder": str(old_folder), "mode": "replace"},
    )
    assert initial.status_code == 200, initial.text

    _audio(old_folder / "Appeared-during-watch.mp3")
    switched = False

    def _switch_profile_during_probe(path, *, fallback_title, require_playable):
        nonlocal switched
        if not switched:
            switched = True
            other = get_connection()
            other.execute(
                "UPDATE station_settings SET value=? "
                "WHERE station_id=1 AND key='music_library_folder'",
                (str(new_folder),),
            )
            other.commit()
            other.close()
        return _fake_metadata(path, fallback_title=fallback_title, require_playable=require_playable)

    monkeypatch.setattr("app.api.legacy._get_audio_metadata", _switch_profile_during_probe)
    stale = client.post(
        "/api/library/folder/sync",
        json={
            "station_id": 1,
            "folder": str(old_folder),
            "mode": "replace",
            "incremental": True,
            "guard_configured_folder": True,
        },
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["skipped"] is True
    assert stale.json()["reason"] == "stale_managed_library_profile"

    conn = get_connection()
    configured = conn.execute(
        "SELECT value FROM station_settings WHERE station_id=1 AND key='music_library_folder'"
    ).fetchone()["value"]
    active = conn.execute(
        "SELECT file_path FROM tracks WHERE station_id=1 AND is_active=1"
    ).fetchall()
    conn.close()
    assert Path(str(configured)).resolve() == new_folder.resolve()
    assert {Path(str(row["file_path"])).resolve() for row in active} == {old_track}
