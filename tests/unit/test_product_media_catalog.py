import os
import sqlite3
import threading
import time

import app.api.library_automation as library_automation
import app.services.product_media_catalog as product_catalog
import pytest

from app.services.product_media_catalog import ProductCatalogError, ProductMediaCatalogService


def _prepare_product_folders(root):
    for relative in ("Broadcast", "Juke/Non-Turkish", "Voting", "Jingles", "Ads", "Emergency"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _catalog_rows(service, product):
    connection = sqlite3.connect(service._io_path(service._database(product)))
    try:
        return connection.execute("SELECT relative_path FROM catalog_items ORDER BY relative_path").fetchall()
    finally:
        connection.close()


def test_product_catalog_settles_then_atomically_reflects_rename_and_delete(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    original = root / "Broadcast" / "nested" / "first.mp3"
    original.parent.mkdir()
    original.write_bytes(b"audio")
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=0, required_stable_polls=2)
    base = time.time() + 2

    service.poll_once(now=base)
    assert service.snapshot()["products"][0]["state"] == "settling"
    service.poll_once(now=base + 1)
    assert _catalog_rows(service, "broadcast") == [("nested/first.mp3",)]

    renamed = original.with_name("renamed.mp3")
    original.rename(renamed)
    service.poll_once(now=base + 2)
    service.poll_once(now=base + 3)
    assert _catalog_rows(service, "broadcast") == [("nested/renamed.mp3",)]

    renamed.unlink()
    service.poll_once(now=base + 4)
    service.poll_once(now=base + 5)
    assert _catalog_rows(service, "broadcast") == []
    snapshot = service.snapshot()
    broadcast = next(item for item in snapshot["products"] if item["product"] == "broadcast")
    assert broadcast["generation"] == 3
    assert "root" not in snapshot
    assert "folder" not in broadcast


def test_product_catalog_requires_quiet_media_and_rescan_works_before_poll(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    media = root / "Jingles" / "fresh.mp3"
    media.write_bytes(b"fresh")
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=60, required_stable_polls=2)

    assert service.request_rescan("jingles") == 1
    assert service.request_rescan() == 6
    service.poll_once(now=time.time())
    service.poll_once(now=time.time() + 1)
    jingle = next(item for item in service.snapshot()["products"] if item["product"] == "jingles")
    assert jingle["state"] == "settling"
    assert not service._database("jingles").exists()


def test_product_catalog_uses_a_safe_env_backed_poll_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIOTEDU_PRODUCT_CATALOG_POLL_SECONDS", "25")
    service = ProductMediaCatalogService(tmp_path / "RadioTEDU Media")
    assert service.snapshot()["poll_interval_seconds"] == 25.0


def test_product_catalog_uses_windows_safe_io_for_deep_paths_and_keeps_last_good_generation(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=0, required_stable_polls=2)
    deep = (root / "Ads").joinpath(*(["segment-" + "x" * 36] * 8))
    os.makedirs(service._io_path(deep), exist_ok=False)
    media = deep / "spot.mp3"
    with open(service._io_path(media), "wb") as handle:
        handle.write(b"spot")
    assert len(str(media)) > 260
    base = time.time() + 2

    service.poll_once(now=base)
    service.poll_once(now=base + 1)
    assert _catalog_rows(service, "ads") == [(media.relative_to(root / "Ads").as_posix(),)]
    last_good = next(item for item in service.snapshot()["products"] if item["product"] == "ads")["last_good_generation"]

    original_scan = service._scan

    def failed_scan(product, now):
        if product == "ads":
            from app.services.product_media_catalog import ProductCatalogError
            raise ProductCatalogError("media_path_unsafe")
        return original_scan(product, now)

    monkeypatch.setattr(service, "_scan", failed_scan)
    service.poll_once(now=base + 2)
    status = next(item for item in service.snapshot()["products"] if item["product"] == "ads")
    assert status["error_code"] == "media_path_unsafe"
    assert status["last_good_generation"] == last_good
    assert _catalog_rows(service, "ads") == [(media.relative_to(root / "Ads").as_posix(),)]


def test_product_catalog_api_status_and_early_rescan_are_scoped_and_safe(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    service = ProductMediaCatalogService(root)
    monkeypatch.setattr(library_automation, "get_product_media_catalog_service", lambda: service)

    status = library_automation.product_catalog_status(_user={})
    queued = library_automation.product_catalog_rescan(
        library_automation.ProductCatalogRescanPayload(product="voting"), _user={}
    )

    assert "root" not in status
    assert queued["queued_products"] == 1
    voting = next(item for item in queued["products"] if item["product"] == "voting")
    assert voting["state"] == "queued"
    assert voting["database"] == "Databases/voting.sqlite3"


def test_existing_watcher_public_snapshot_redacts_paths_and_raw_errors():
    public = library_automation._public_watcher_snapshot(
        {
            "running": True,
            "profiles": [
                {
                    "station_id": 1,
                    "track_type": "music",
                    "folder": r"E:\RadioTEDU Media\Broadcast",
                    "status": "retry_wait",
                    "error": r"Folder unavailable: E:\private",
                    "result": {"folder": r"E:\RadioTEDU Media\Broadcast", "file_count": 7, "verified": True},
                }
            ],
        }
    )
    profile = public["profiles"][0]
    assert profile["error_code"] == "managed_library_sync_failed"
    assert profile["result"] == {"file_count": 7, "verified": True}
    assert "E:" not in str(public)


def test_catalog_worker_survives_pre_transaction_filesystem_failure(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    (root / "Broadcast" / "song.mp3").write_bytes(b"song")
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    base = time.time() + 2
    service.poll_once(now=base)
    real_makedirs = product_catalog.os.makedirs

    def fail_catalog_mkdir(path, *args, **kwargs):
        if ".catalog-locks" in str(path):
            raise OSError("simulated lock directory failure")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(product_catalog.os, "makedirs", fail_catalog_mkdir)
    service.poll_once(now=base + 1)
    broadcast = next(item for item in service.snapshot()["products"] if item["product"] == "broadcast")
    assert broadcast["state"] == "retry_wait"
    assert broadcast["error_code"] == "catalog_lock_unavailable"


def test_catalog_cross_process_lock_rejects_a_second_writer(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    media = root / "Broadcast" / "song.mp3"
    media.write_bytes(b"song")
    first = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    second = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    fingerprint, records, quiet = first._scan("broadcast", time.time() + 2)
    assert quiet

    with first._product_file_lock("broadcast"):
        with pytest.raises(ProductCatalogError, match="catalog_lock_busy"):
            second._write_generation("broadcast", fingerprint, records)


def test_catalog_revalidation_rolls_back_changed_snapshot_and_boot_reuses_last_good(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    media = root / "Broadcast" / "first.mp3"
    media.write_bytes(b"first")
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    base = time.time() + 2
    service.poll_once(now=base)
    service.poll_once(now=base + 1)
    before_generation, before_fingerprint = service._load_existing_catalog("broadcast")
    fingerprint, records, quiet = service._scan("broadcast", time.time() + 2)
    assert quiet and fingerprint == before_fingerprint
    real_scan = service._scan
    calls = 0

    def changed_before_commit(product, now):
        nonlocal calls
        calls += 1
        if product == "broadcast" and calls == 2:
            return "changed", records, True
        return real_scan(product, now)

    monkeypatch.setattr(service, "_scan", changed_before_commit)
    with pytest.raises(ProductCatalogError, match="catalog_snapshot_changed"):
        service._write_generation("broadcast", fingerprint, records)
    assert service._load_existing_catalog("broadcast") == (before_generation, before_fingerprint)

    restarted = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    restarted.reconcile_once()
    status = next(item for item in restarted.snapshot()["products"] if item["product"] == "broadcast")
    assert status["last_good_generation"] == before_generation
    assert status["generation"] == before_generation


def test_catalog_failure_uses_slow_recovery_after_rapid_retry_budget(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    service = ProductMediaCatalogService(root, max_retries=0)
    real_scan = service._scan
    attempts = 0

    def fail_broadcast(product, now):
        nonlocal attempts
        if product == "broadcast":
            attempts += 1
            raise ProductCatalogError("media_scan_failed")
        return real_scan(product, now)

    monkeypatch.setattr(service, "_scan", fail_broadcast)
    service.poll_once(now=100)
    service.poll_once(now=101)
    status = next(item for item in service.snapshot()["products"] if item["product"] == "broadcast")
    assert attempts == 1
    assert status["state"] == "retry_wait"
    service.poll_once(now=401)
    assert attempts == 2


def test_catalog_rejects_reparse_root_before_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(ProductMediaCatalogService, "_exists_static", staticmethod(lambda _path: True))
    monkeypatch.setattr(ProductMediaCatalogService, "_reparse_or_symlink_static", staticmethod(lambda _path: True))
    with pytest.raises(ProductCatalogError, match="media_root_unsafe"):
        ProductMediaCatalogService(tmp_path / "operator-writable-root")


def test_product_catalog_databases_are_isolated_per_fixed_folder(tmp_path):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    (root / "Broadcast" / "program.mp3").write_bytes(b"program")
    (root / "Voting" / "vote.mp3").write_bytes(b"vote")
    service = ProductMediaCatalogService(root, minimum_quiet_seconds=0)
    base = time.time() + 2
    service.poll_once(now=base)
    service.poll_once(now=base + 1)
    assert _catalog_rows(service, "broadcast") == [("program.mp3",)]
    assert _catalog_rows(service, "voting") == [("vote.mp3",)]
    assert service._database("broadcast") != service._database("voting")


def test_catalog_start_loads_metadata_without_sync_scanning_and_stops_promptly(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    service = ProductMediaCatalogService(root)
    entered = threading.Event()

    def blocked_run():
        entered.set()
        service._stop_event.wait(2)

    monkeypatch.setattr(service, "_run", blocked_run)
    monkeypatch.setattr(service, "_scan", lambda *_args: pytest.fail("start must not scan media"))
    service.start()
    assert entered.wait(1)
    service.stop(timeout=1)
    assert not service._thread.is_alive()


def test_catalog_daemon_waits_before_first_scan_and_rescan_wakes_it(tmp_path, monkeypatch):
    root = tmp_path / "RadioTEDU Media"
    _prepare_product_folders(root)
    service = ProductMediaCatalogService(root, poll_interval_seconds=30)
    scanned = threading.Event()

    def scan_once(*_args, **_kwargs):
        scanned.set()

    monkeypatch.setattr(service, "poll_once", scan_once)
    service.start()
    assert not scanned.wait(0.1)
    service.request_rescan("broadcast")
    assert scanned.wait(1)
    service.stop(timeout=1)
