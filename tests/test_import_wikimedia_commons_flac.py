from pathlib import Path

import pytest

from tools import import_wikimedia_commons_flac as importer


def _commons_payload(license_name: str, license_url: str) -> dict:
    return {
        "query": {
            "pages": [
                {
                    "title": "File:Candidate.flac",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/candidate.flac",
                            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Candidate.flac",
                            "mime": "audio/flac",
                            "size": 123,
                            "sha1": "a" * 40,
                            "thumburl": "https://upload.wikimedia.org/candidate.png",
                            "thumbmime": "image/png",
                            "extmetadata": {
                                "LicenseShortName": {"value": license_name},
                                "LicenseUrl": {"value": license_url},
                                "Artist": {"value": "Example Artist"},
                            },
                        }
                    ],
                }
            ]
        }
    }


def test_candidate_root_maps_radio_to_pop_without_database(tmp_path: Path) -> None:
    root = tmp_path / "RadioTEDU Creative Commons Candidates"

    libraries = importer._candidate_libraries(root)

    assert libraries["radio"] == root.resolve() / "Pop"
    assert libraries["cazz"] == root.resolve() / "Jazz"
    assert all(root.resolve() in folder.parents for folder in libraries.values())


def test_candidate_root_rejects_live_library_name(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must not be the live"):
        importer._candidate_libraries(tmp_path / "RadioTEDU Songs")


def test_commons_license_rejects_noncommercial_without_explicit_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        importer,
        "_request_json",
        lambda _params: _commons_payload(
            "CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0"
        ),
    )

    with pytest.raises(RuntimeError, match="not suitable"):
        importer._commons_info("File:Candidate.flac")


def test_commons_license_accepts_attribution_and_records_preview(monkeypatch) -> None:
    monkeypatch.setattr(
        importer,
        "_request_json",
        lambda _params: _commons_payload(
            "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0"
        ),
    )

    info = importer._commons_info("File:Candidate.flac")

    assert info["license"] == "CC BY 4.0"
    assert info["preview_url"].endswith("candidate.png")
    assert info["preview_mime"] == "image/png"
