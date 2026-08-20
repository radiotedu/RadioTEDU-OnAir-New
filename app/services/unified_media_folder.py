from __future__ import annotations

import json
import ntpath
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.reliability import atomic_write_json, read_json_object


DEFAULT_MEDIA_ROOT = Path("E:/RadioTEDU Media")
SOURCE_MAP_FILENAME = "source-map.json"
MANIFEST_FILENAME = "unified-media-manifest.json"
STATUS_FILENAME = "unified-media-status.json"
LAYOUT_DIRECTORIES = (
    "Broadcast",
    "Juke/Non-Turkish",
    "Voting",
    "Jingles",
    "Ads",
    "Emergency",
    "Imports",
    "Sources",
    "Archive",
    "Databases",
    "Manifests",
    "Backups",
)
VIEW_DIRECTORIES = {
    "broadcast": "Broadcast",
    "juke_non_turkish": "Juke/Non-Turkish",
    "voting": "Voting",
    "jingles": "Jingles",
    "ads": "Ads",
    "emergency": "Emergency",
}
_INTERNAL_DIRECTORY = ".unified-media"


class UnifiedMediaFolderError(ValueError):
    """Raised when a media view request cannot be safely published."""


@dataclass(frozen=True)
class _SourceSpec:
    source: Path
    destination: Path
    language: str


class UnifiedMediaFolderService:
    """Build hardlink-only library views from an explicit local source map.

    The service never discovers or categorizes source media.  Each source-map
    entry must identify its language and source directory explicitly, which
    avoids accidental language or playout routing decisions.  All sources,
    staging, views, manifests, and rollback directories stay below one root
    and on one volume, so publishing cannot copy media across disks.
    """

    def __init__(self, root: str | Path | None = None):
        configured = str(root or os.getenv("RADIOTEDU_MEDIA_ROOT", "")).strip()
        self.root = Path(configured) if configured else DEFAULT_MEDIA_ROOT
        if not self.root.is_absolute():
            raise UnifiedMediaFolderError("media_root_must_be_absolute")
        self.root = self.root.expanduser().resolve(strict=False)

    @property
    def manifests_dir(self) -> Path:
        return self.root / "Manifests"

    @property
    def source_map_path(self) -> Path:
        return self.manifests_dir / SOURCE_MAP_FILENAME

    @property
    def manifest_path(self) -> Path:
        return self.manifests_dir / MANIFEST_FILENAME

    @property
    def status_path(self) -> Path:
        return self.manifests_dir / STATUS_FILENAME

    @property
    def _internal_root(self) -> Path:
        return self.manifests_dir / _INTERNAL_DIRECTORY

    def ensure_layout(self) -> None:
        self._mkdir(self.root, parents=True, exist_ok=True)
        self._assert_same_volume(self.root)
        for relative in LAYOUT_DIRECTORIES:
            self._mkdir(
                self._resolve_relative(relative, field="layout_directory"),
                parents=True,
                exist_ok=True,
            )
        self._mkdir(self._internal_root / "staging", parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        manifest = read_json_object(self.manifest_path)
        refresh_status = read_json_object(self.status_path)
        manifest_counts = manifest.get("counts", {}) if isinstance(manifest, dict) else {}
        views = []
        for view, relative in VIEW_DIRECTORIES.items():
            target = self._resolve_relative(relative, field="view")
            view_counts = manifest_counts.get(view, {}) or {}
            generated_count = int(view_counts.get("generated", 0))
            operator_count = int(view_counts.get("operator", 0))
            views.append(
                {
                    "view": view,
                    "directory": relative.replace("/", "\\"),
                    "exists": self._is_dir(target),
                    # Status is polled by the operator every few seconds.  Use
                    # the last published manifest instead of recursively walking
                    # every media view on each request; refresh() updates these
                    # counts atomically when a new view is published.
                    "file_count": generated_count + operator_count,
                    "generated_count": generated_count,
                    "operator_count": operator_count,
                }
            )
        return {
            "root": str(self.root),
            "layout_ready": all(
                self._is_dir(self._resolve_relative(relative, field="layout_directory"))
                for relative in LAYOUT_DIRECTORIES
            ),
            "source_map_path": str(self.source_map_path),
            "source_map_configured": self._is_file(self.source_map_path),
            "manifest_path": str(self.manifest_path),
            "last_published_at": str(manifest.get("published_at") or "")
            if isinstance(manifest, dict)
            else "",
            "last_refresh_at": str(refresh_status.get("last_refresh_at") or "")
            if isinstance(refresh_status, dict)
            else "",
            "last_error": str(refresh_status.get("last_error") or "")[:160]
            if isinstance(refresh_status, dict)
            else "",
            "last_run_id": str(manifest.get("run_id") or "")
            if isinstance(manifest, dict)
            else "",
            "views": views,
        }

    def refresh(self) -> dict[str, Any]:
        staging_root: Path | None = None
        try:
            self.ensure_layout()
            source_specs = self._load_source_map()
            run_id = uuid.uuid4().hex
            staging_root = self._resolve_relative(
                f"Manifests/{_INTERNAL_DIRECTORY}/staging/{run_id}", field="staging"
            )
            rollback_root = self._resolve_relative(
                f"Backups/unified-media-{run_id}", field="rollback"
            )
            self._mkdir(staging_root, parents=True, exist_ok=False)
            self._mkdir(rollback_root, parents=True, exist_ok=False)
            manifest_entries: list[dict[str, Any]] = []
            prior_generated = self._previous_generated_destinations()
            for view, specs in source_specs.items():
                staged_view = staging_root / view
                self._mkdir(staged_view, parents=True, exist_ok=False)
                generated = self._build_staged_view(view, specs, staged_view)
                operator = self._carry_operator_files(
                    view, staged_view, {entry["destination"].casefold() for entry in generated}, prior_generated.get(view, set())
                )
                manifest_entries.extend(generated)
                manifest_entries.extend(operator)
            self._publish_views(run_id, staging_root, rollback_root, source_specs)
        except UnifiedMediaFolderError as exc:
            if staging_root is not None:
                self._cleanup_staging(staging_root)
            self._record_refresh_status(error=str(exc))
            raise
        except OSError as exc:
            if staging_root is not None:
                self._cleanup_staging(staging_root)
            self._record_refresh_status(error="media_refresh_failed")
            raise UnifiedMediaFolderError("media_refresh_failed") from exc

        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "root": str(self.root),
            "hardlink_only": True,
            "entries": manifest_entries,
            "counts": {
                view: {
                    "generated": sum(1 for item in manifest_entries if item["view"] == view and item["ownership"] == "generated"),
                    "operator": sum(1 for item in manifest_entries if item["view"] == view and item["ownership"] == "operator"),
                }
                for view in VIEW_DIRECTORIES
            },
        }
        atomic_write_json(self.manifest_path, manifest)
        self._record_refresh_status(error="", refreshed_at=manifest["published_at"])
        self._cleanup_staging(staging_root)
        return {
            "ok": True,
            "run_id": run_id,
            "published_at": manifest["published_at"],
            "views": {
                view: sum(1 for item in manifest_entries if item["view"] == view)
                for view in VIEW_DIRECTORIES
            },
            "manifest_path": str(self.manifest_path),
        }

    def _record_refresh_status(self, *, error: str, refreshed_at: str | None = None) -> None:
        try:
            self._mkdir(self.manifests_dir, parents=True, exist_ok=True)
            atomic_write_json(
                self.status_path,
                {
                    "last_refresh_at": refreshed_at
                    or datetime.now(timezone.utc).isoformat(),
                    "last_error": str(error or "")[:160],
                },
            )
        except OSError:
            # Publishing errors must retain their original, stable failure code.
            pass

    def _load_source_map(self) -> dict[str, list[_SourceSpec]]:
        if not self._is_file(self.source_map_path):
            raise UnifiedMediaFolderError("source_map_not_found")
        try:
            payload = json.loads(self.source_map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnifiedMediaFolderError("source_map_invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise UnifiedMediaFolderError("source_map_version_invalid")
        raw_views = payload.get("views")
        if not isinstance(raw_views, list) or not raw_views:
            raise UnifiedMediaFolderError("source_map_views_required")
        parsed: dict[str, list[_SourceSpec]] = {}
        for raw_view in raw_views:
            if not isinstance(raw_view, dict):
                raise UnifiedMediaFolderError("source_map_view_invalid")
            view = str(raw_view.get("view") or "").strip()
            if view not in VIEW_DIRECTORIES or view in parsed:
                raise UnifiedMediaFolderError("source_map_view_unknown_or_duplicate")
            raw_sources = raw_view.get("sources")
            if not isinstance(raw_sources, list) or not raw_sources:
                raise UnifiedMediaFolderError("source_map_sources_required")
            specs: list[_SourceSpec] = []
            for raw_source in raw_sources:
                if not isinstance(raw_source, dict):
                    raise UnifiedMediaFolderError("source_map_source_invalid")
                source_value = str(raw_source.get("source") or "").strip()
                language = str(raw_source.get("language") or "").strip()
                destination_value = str(raw_source.get("destination") or "").strip()
                if not source_value or not language:
                    raise UnifiedMediaFolderError("source_map_source_and_language_required")
                source = self._resolve_relative(source_value, field="source")
                if not self._is_dir(source) or self._is_symlink(source):
                    raise UnifiedMediaFolderError("source_directory_not_ready")
                if any(
                    self._is_within(source, self._resolve_relative(relative, field="view"))
                    for relative in VIEW_DIRECTORIES.values()
                ):
                    raise UnifiedMediaFolderError("source_directory_must_not_be_a_view")
                destination = (
                    self._relative_path(destination_value, field="destination")
                    if destination_value
                    else Path(".")
                )
                specs.append(_SourceSpec(source=source, destination=destination, language=language))
            parsed[view] = specs
        return parsed

    def _build_staged_view(
        self, view: str, specs: list[_SourceSpec], staged_view: Path
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        destinations: set[str] = set()
        for spec in specs:
            self._assert_same_volume(spec.source)
            for source_file in self._iter_source_files(spec.source):
                relative = source_file.relative_to(spec.source)
                target_relative = spec.destination / relative
                destination_key = str(target_relative).replace("\\", "/").casefold()
                if destination_key in destinations:
                    raise UnifiedMediaFolderError("source_map_target_collision")
                destinations.add(destination_key)
                destination = staged_view / target_relative
                self._mkdir(destination.parent, parents=True, exist_ok=True)
                self._assert_same_volume(source_file)
                try:
                    os.link(self._io_path(source_file), self._io_path(destination))
                except OSError as exc:
                    raise UnifiedMediaFolderError("hardlink_publish_failed") from exc
                source_stat = self._stat(source_file)
                destination_stat = self._stat(destination)
                if source_stat.st_ino != destination_stat.st_ino:
                    raise UnifiedMediaFolderError("hardlink_verification_failed")
                entries.append(
                    {
                        "view": view,
                        "source": str(source_file.relative_to(self.root)).replace("\\", "/"),
                        "destination": str(target_relative).replace("\\", "/"),
                        "language": spec.language,
                        "size": int(source_stat.st_size),
                        "ownership": "generated",
                    }
                )
        return entries

    def _previous_generated_destinations(self) -> dict[str, set[str]]:
        manifest = read_json_object(self.manifest_path)
        result: dict[str, set[str]] = {view: set() for view in VIEW_DIRECTORIES}
        if not isinstance(manifest, dict):
            return result
        for entry in manifest.get("entries", []):
            if not isinstance(entry, dict) or entry.get("ownership", "generated") != "generated":
                continue
            view = entry.get("view")
            destination = entry.get("destination")
            if view in result and isinstance(destination, str):
                result[view].add(destination.replace("\\", "/").casefold())
        return result

    def _carry_operator_files(
        self, view: str, staged_view: Path, generated: set[str], prior_generated: set[str]
    ) -> list[dict[str, Any]]:
        target = self._resolve_relative(VIEW_DIRECTORIES[view], field="view")
        carried: list[dict[str, Any]] = []
        if not self._is_dir(target) or self._is_symlink(target):
            return carried
        self._assert_same_volume(target)
        for operator_file in self._iter_source_files(target):
            relative = operator_file.relative_to(target)
            key = str(relative).replace("\\", "/").casefold()
            if key in prior_generated:
                continue
            if key in generated:
                raise UnifiedMediaFolderError("operator_file_target_collision")
            destination = staged_view / relative
            self._mkdir(destination.parent, parents=True, exist_ok=True)
            self._assert_same_volume(operator_file)
            try:
                os.link(self._io_path(operator_file), self._io_path(destination))
            except OSError as exc:
                raise UnifiedMediaFolderError("operator_hardlink_publish_failed") from exc
            source_stat = self._stat(operator_file)
            if source_stat.st_ino != self._stat(destination).st_ino:
                raise UnifiedMediaFolderError("operator_hardlink_verification_failed")
            carried.append({"view": view, "source": str(relative).replace("\\", "/"), "destination": str(relative).replace("\\", "/"), "language": "operator", "size": int(source_stat.st_size), "ownership": "operator"})
        return carried

    def _publish_views(
        self,
        run_id: str,
        staging_root: Path,
        rollback_root: Path,
        source_specs: dict[str, list[_SourceSpec]],
    ) -> None:
        published: list[tuple[Path, Path]] = []
        try:
            for view in source_specs:
                target = self._resolve_relative(VIEW_DIRECTORIES[view], field="view")
                staged = staging_root / view
                backup = rollback_root / view
                if self._exists(target):
                    os.replace(self._io_path(target), self._io_path(backup))
                os.replace(self._io_path(staged), self._io_path(target))
                published.append((target, backup))
        except OSError as exc:
            for target, backup in reversed(published):
                try:
                    if self._exists(target):
                        failed_target = rollback_root / f"failed-{target.name}-{run_id}"
                        os.replace(self._io_path(target), self._io_path(failed_target))
                    if self._exists(backup):
                        os.replace(self._io_path(backup), self._io_path(target))
                except OSError:
                    pass
            raise UnifiedMediaFolderError("atomic_publish_failed") from exc

    def _resolve_relative(self, raw: str, *, field: str) -> Path:
        relative = self._relative_path(raw, field=field)
        candidate = (self.root / relative).resolve(strict=False)
        if not self._is_within(candidate, self.root):
            raise UnifiedMediaFolderError(f"{field}_outside_media_root")
        return candidate

    @staticmethod
    def _relative_path(raw: str, *, field: str) -> Path:
        value = Path(str(raw).strip())
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise UnifiedMediaFolderError(f"{field}_must_be_relative_and_contained")
        return value

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    def _assert_same_volume(self, candidate: Path) -> None:
        try:
            if self._stat(candidate).st_dev != self._stat(self.root).st_dev:
                raise UnifiedMediaFolderError("media_path_on_other_volume")
        except OSError as exc:
            raise UnifiedMediaFolderError("media_path_not_accessible") from exc

    @staticmethod
    def _io_path(path: Path | str) -> str:
        return UnifiedMediaFolderService._windows_io_path(path)

    @staticmethod
    def _windows_io_path(path: Path | str, *, windows: bool | None = None) -> str:
        """Return a Windows long-path-safe spelling for filesystem operations.

        ``pathlib`` retains the operator-facing logical path, while all direct
        filesystem calls use this form.  The prefix is a Windows-only detail;
        POSIX paths are deliberately returned unchanged.
        """
        raw = os.fspath(path)
        if not (os.name == "nt" if windows is None else windows) or raw.startswith("\\\\?\\"):
            return raw
        normalized = ntpath.normpath(ntpath.abspath(raw))
        if normalized.startswith("\\\\"):
            return "\\\\?\\UNC\\" + normalized[2:]
        return "\\\\?\\" + normalized

    def _mkdir(self, path: Path, *, parents: bool, exist_ok: bool) -> None:
        if parents:
            os.makedirs(self._io_path(path), exist_ok=exist_ok)
        else:
            os.mkdir(self._io_path(path))

    def _stat(self, path: Path) -> os.stat_result:
        return os.stat(self._io_path(path))

    def _exists(self, path: Path) -> bool:
        return os.path.exists(self._io_path(path))

    def _is_dir(self, path: Path) -> bool:
        try:
            return stat.S_ISDIR(self._stat(path).st_mode)
        except OSError:
            return False

    def _is_file(self, path: Path) -> bool:
        try:
            return stat.S_ISREG(self._stat(path).st_mode)
        except OSError:
            return False

    def _is_symlink(self, path: Path) -> bool:
        return os.path.islink(self._io_path(path))

    def _cleanup_staging(self, path: Path) -> None:
        """Remove only the per-run staging directory, never a broader root."""
        staging_parent = self._internal_root / "staging"
        if path.parent != staging_parent or path.name in {"", ".", ".."}:
            return
        shutil.rmtree(self._io_path(path), ignore_errors=True)

    def _iter_source_files(self, source: Path):
        source_io = self._io_path(source)
        for current, directories, filenames in os.walk(source_io, topdown=True, followlinks=False):
            directories.sort()
            filenames.sort()
            for directory in directories:
                if self._is_symlink(Path(current) / directory):
                    raise UnifiedMediaFolderError("source_symlink_not_allowed")
            for filename in filenames:
                candidate_io = os.path.join(current, filename)
                if os.path.islink(candidate_io):
                    raise UnifiedMediaFolderError("source_symlink_not_allowed")
                if self._is_file(Path(candidate_io)):
                    relative = Path(os.path.relpath(candidate_io, source_io))
                    yield source / relative

    def _file_count(self, folder: Path) -> int:
        if not self._is_dir(folder):
            return 0
        count = 0
        for current, directories, filenames in os.walk(self._io_path(folder), topdown=True, followlinks=False):
            directories[:] = [
                directory
                for directory in directories
                if not os.path.islink(os.path.join(current, directory))
            ]
            count += sum(
                1
                for filename in filenames
                if not os.path.islink(os.path.join(current, filename))
                and self._is_file(Path(os.path.join(current, filename)))
            )
        return count


_unified_media_folder_service: UnifiedMediaFolderService | None = None


def get_unified_media_folder_service() -> UnifiedMediaFolderService:
    global _unified_media_folder_service
    if _unified_media_folder_service is None:
        _unified_media_folder_service = UnifiedMediaFolderService()
    return _unified_media_folder_service
