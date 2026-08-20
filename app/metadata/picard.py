MB_FIELDS = {
    "musicbrainz_recordingid",
    "musicbrainz_trackid",
    "musicbrainz_albumid",
    "musicbrainz_artistid",
    "musicbrainz_releasegroupid",
}


def normalize_metadata(meta: dict) -> dict:
    out = {}
    for key, value in (meta or {}).items():
        if key in MB_FIELDS:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value.strip()
        else:
            out[key] = value
    return out
