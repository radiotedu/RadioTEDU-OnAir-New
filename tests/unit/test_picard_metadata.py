from app.metadata.picard import normalize_metadata


def test_musicbrainz_ids_are_preserved_by_default():
    inp = {
        "title": " Song ",
        "artist": " Artist ",
        "musicbrainz_recordingid": "mb-recording-1",
        "musicbrainz_albumid": "mb-album-1",
    }
    out = normalize_metadata(inp)
    assert out["title"] == "Song"
    assert out["artist"] == "Artist"
    assert out["musicbrainz_recordingid"] == "mb-recording-1"
    assert out["musicbrainz_albumid"] == "mb-album-1"
