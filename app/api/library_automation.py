from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import require_permission
from app.services.managed_library_watcher import get_managed_library_watcher
from app.services.product_media_catalog import (
    ProductCatalogError,
    get_product_media_catalog_service,
)
from app.services.unified_media_folder import (
    UnifiedMediaFolderError,
    get_unified_media_folder_service,
)

router = APIRouter(tags=["library-automation"])


class ManagedLibraryRescanPayload(BaseModel):
    station_id: int | None = None
    track_type: str | None = None


class UnifiedMediaRefreshPayload(BaseModel):
    request_library_rescan: bool = True


class ProductCatalogRescanPayload(BaseModel):
    product: str | None = None


def _public_watcher_snapshot(snapshot: dict) -> dict:
    """Keep internal watcher diagnostics private from the operator API."""
    profiles = []
    allowed_result_keys = {
        "verified",
        "file_count",
        "expected_files",
        "active_files",
        "added",
        "reactivated",
        "retained",
        "deactivated",
        "duplicate_rows_deactivated",
        "invalid_files_skipped",
        "pending_queue_items_removed",
        "program_queue_items_removed",
        "pending_schedules_removed",
    }
    for profile in list(snapshot.get("profiles") or []):
        raw_error = str(profile.get("error") or "")
        raw_result = dict(profile.get("result") or {})
        profiles.append(
            {
                "station_id": profile.get("station_id"),
                "track_type": profile.get("track_type"),
                "recursive": bool(profile.get("recursive")),
                "mode": profile.get("mode"),
                "status": profile.get("status"),
                "last_scan_at": profile.get("last_scan_at"),
                "last_sync_at": profile.get("last_sync_at"),
                "retry_count": profile.get("retry_count"),
                "error_code": "managed_library_sync_failed" if raw_error else "",
                "result": {
                    key: value
                    for key, value in raw_result.items()
                    if key in allowed_result_keys and isinstance(value, (bool, int, float))
                },
            }
        )
    return {"running": bool(snapshot.get("running")), "profiles": profiles}


@router.get("/api/library/watcher/status")
def managed_library_watcher_status(_user=Depends(require_permission("stations.edit"))):
    return _public_watcher_snapshot(get_managed_library_watcher().snapshot())


@router.post("/api/library/watcher/rescan")
def managed_library_watcher_rescan(
    payload: ManagedLibraryRescanPayload,
    _user=Depends(require_permission("stations.edit")),
):
    watcher = get_managed_library_watcher()
    selected = watcher.request_rescan(
        station_id=payload.station_id,
        track_type=payload.track_type,
    )
    return {"ok": True, "queued_profiles": selected, **_public_watcher_snapshot(watcher.snapshot())}


@router.get("/api/library/product-catalog/status")
def product_catalog_status(_user=Depends(require_permission("stations.edit"))):
    return get_product_media_catalog_service().snapshot()


@router.post("/api/library/product-catalog/rescan")
def product_catalog_rescan(
    payload: ProductCatalogRescanPayload,
    _user=Depends(require_permission("stations.edit")),
):
    service = get_product_media_catalog_service()
    try:
        queued = service.request_rescan(payload.product)
    except ProductCatalogError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "queued_products": queued, **service.snapshot()}


@router.get("/api/library/unified-media/status")
def unified_media_status(_user=Depends(require_permission("stations.edit"))):
    """Return the fixed media-root layout without reading source media."""
    try:
        return get_unified_media_folder_service().status()
    except UnifiedMediaFolderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/library/unified-media/refresh")
def refresh_unified_media(
    payload: UnifiedMediaRefreshPayload,
    _user=Depends(require_permission("stations.edit")),
):
    """Publish explicit hardlink views, then request existing library syncs."""
    try:
        result = get_unified_media_folder_service().refresh()
    except UnifiedMediaFolderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    watcher = get_managed_library_watcher()
    queued = watcher.request_rescan() if payload.request_library_rescan else 0
    return {
        **result,
        "library_rescan_queued_profiles": queued,
        "watcher": _public_watcher_snapshot(watcher.snapshot()),
    }
