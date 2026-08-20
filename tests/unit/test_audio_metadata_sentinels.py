from app.api.legacy import _clean_artist_metadata


def test_stream_error_metadata_is_not_presented_as_an_artist():
    assert _clean_artist_metadata("stream_error") == ""
    assert _clean_artist_metadata(" STREAM-ERROR ") == ""


def test_real_artist_metadata_is_preserved():
    assert _clean_artist_metadata("Boards of Canada") == "Boards of Canada"
