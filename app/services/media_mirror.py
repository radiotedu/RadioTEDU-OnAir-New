from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from app.db import get_connection, init_db
from app.runtime_paths import get_data_dir


def _media_root() -> Path:
    configured = os.getenv("CLEANROOM_MEDIA_ROOT", "").strip()
    return Path(configured).resolve() if configured else (get_data_dir() / "Media").resolve()


class MediaMirrorService:
    def manifest(self, root: str | Path | None = None) -> dict:
        base = Path(root).resolve() if root else _media_root()
        items = []
        total = 0
        if base.exists():
            for path in sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink()):
                relative = path.relative_to(base).as_posix()
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                size = path.stat().st_size
                total += size
                items.append({"path": relative, "size": size, "sha256": digest.hexdigest()})
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
        return {
            "root": str(base),
            "manifest_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "item_count": len(items),
            "total_bytes": total,
            "items": items,
        }

    def compare(self, expected: dict, root: str | Path | None = None) -> dict:
        actual = self.manifest(root)
        expected_items = {str(item["path"]): item for item in expected.get("items", [])}
        actual_items = {str(item["path"]): item for item in actual["items"]}
        missing = sorted(set(expected_items) - set(actual_items))
        extra = sorted(set(actual_items) - set(expected_items))
        changed = sorted(
            path for path in set(expected_items) & set(actual_items)
            if expected_items[path].get("sha256") != actual_items[path].get("sha256")
        )
        return {"ready": not missing and not changed, "missing": missing, "changed": changed, "extra": extra, "actual": actual}

    def record(self, node_id: str, root: str | Path | None = None) -> dict:
        init_db()
        manifest = self.manifest(root)
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO media_manifests(node_id, root_path, manifest_hash, item_count, total_bytes, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, manifest["root"], manifest["manifest_hash"], manifest["item_count"], manifest["total_bytes"], json.dumps(manifest, separators=(",", ":"))),
            )
            conn.commit()
        finally:
            conn.close()
        return manifest

    def synchronize(self, source_root: str | Path, destination_root: str | Path | None = None) -> dict:
        source = Path(source_root).resolve()
        destination = Path(destination_root).resolve() if destination_root else _media_root()
        if not source.is_dir() or source == destination:
            raise ValueError("invalid_media_mirror_source")
        expected = self.manifest(source)
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            for item in expected["items"]:
                relative = Path(str(item["path"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe_media_manifest_path")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
            verified = self.compare(expected, staging)
            if not verified["ready"] or verified["extra"]:
                raise RuntimeError("media_mirror_verification_failed")
            previous = destination.parent / f".{destination.name}.previous"
            if previous.exists():
                shutil.rmtree(previous)
            if destination.exists():
                os.replace(destination, previous)
            os.replace(staging, destination)
            if previous.exists():
                shutil.rmtree(previous)
            return self.compare(expected, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


media_mirror_service = MediaMirrorService()
