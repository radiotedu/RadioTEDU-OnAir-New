import pytest
from fastapi import HTTPException

from app.api import legacy


@pytest.mark.parametrize(
    ("picker", "payload"),
    [
        (legacy.pick_operator_folder, legacy.FolderPickerPayload()),
        (legacy.pick_operator_file, legacy.FilePickerPayload()),
    ],
)
def test_windows_service_picker_fails_fast_with_desktop_guidance(
    monkeypatch, picker, payload
):
    monkeypatch.setattr(
        legacy, "_native_picker_requires_desktop_bridge", lambda: True
    )

    with pytest.raises(HTTPException) as exc_info:
        picker(payload)

    assert exc_info.value.status_code == 409
    assert "RadioTEDU OnAir desktop app" in str(exc_info.value.detail)
    assert "absolute path" in str(exc_info.value.detail)
