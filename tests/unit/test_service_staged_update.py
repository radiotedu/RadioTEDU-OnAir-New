from __future__ import annotations

from pathlib import Path

from tools import run_radio_backend_service as service_runner


def _prepare(repo: Path, data_root: Path) -> Path:
    stage = data_root / "staged" / service_runner._STAGED_UPDATE_ID
    for relative in service_runner._STAGED_UPDATE_FILES:
        current = repo / relative
        source = stage / relative
        current.parent.mkdir(parents=True, exist_ok=True)
        source.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("value = 'old'\n", encoding="utf-8")
        source.write_text("value = 'new'\n", encoding="utf-8")
    (stage / ".apply-on-next-start").touch()
    return stage


def test_pending_update_is_validated_backed_up_and_applied_once(tmp_path) -> None:
    repo = tmp_path / "repo"
    data_root = tmp_path / "data"
    stage = _prepare(repo, data_root)

    assert service_runner.apply_pending_staged_update(repo, data_root) is True
    assert not (stage / ".apply-on-next-start").exists()
    assert (stage / ".applied").is_file()
    for relative in service_runner._STAGED_UPDATE_FILES:
        assert (repo / relative).read_text(encoding="utf-8") == "value = 'new'\n"
        saved = (
            data_root
            / "backups"
            / service_runner._STAGED_UPDATE_ID
            / "preapply-live"
            / relative
        )
        assert saved.read_text(encoding="utf-8") == "value = 'old'\n"
    assert service_runner.apply_pending_staged_update(repo, data_root) is False


def test_partial_copy_failure_restores_old_source_and_keeps_pending_marker(
    tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    data_root = tmp_path / "data"
    stage = _prepare(repo, data_root)
    real_copy = service_runner.shutil.copy2
    failed_relative = service_runner._STAGED_UPDATE_FILES[1]

    def flaky_copy(source, target, *args, **kwargs):
        if Path(source) == stage / failed_relative:
            raise OSError("simulated copy failure")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(service_runner.shutil, "copy2", flaky_copy)

    assert service_runner.apply_pending_staged_update(repo, data_root) is False
    assert (stage / ".apply-on-next-start").is_file()
    assert not (stage / ".applied").exists()
    for relative in service_runner._STAGED_UPDATE_FILES:
        assert (repo / relative).read_text(encoding="utf-8") == "value = 'old'\n"
