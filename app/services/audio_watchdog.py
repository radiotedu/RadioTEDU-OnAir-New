from __future__ import annotations

import json
import logging
import ntpath
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_data_root
from app.db import get_connection, init_db
from app.services.broadcast_campaign import BroadcastCampaignService


_log = logging.getLogger("cleanroom.audio_watchdog")
WATCHDOG_STATIONS = {
    1: ("classical", "http://stream.radiotedu.com:11154/classic"),
    2: ("lofi", "http://stream.radiotedu.com:11154/lofi"),
    5: ("jazz", "http://stream.radiotedu.com:11154/cazz"),
    9: ("energize", "http://stream.radiotedu.com:11154/energize"),
    4: ("pop", "http://stream.radiotedu.com:11154/radio"),
    8: ("rock", "http://stream.radiotedu.com:11154/rock"),
}
CAMPAIGN_STATION_IDS = (1, 4, 8, 9)


def _truthy(raw: Any, default: bool = False) -> bool:
    token = str(raw or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _same_windows_path(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        raw = str(value or "").strip().replace("/", "\\")
        if not raw:
            return ""
        return ntpath.normcase(ntpath.normpath(raw))

    return bool(normalize(left)) and normalize(left) == normalize(right)


class AudioWatchdogService:
    @property
    def state_root(self) -> Path:
        return get_data_root() / "watchdog"

    @property
    def report_path(self) -> Path:
        return self.state_root / "last-run.json"

    def _last_report(self) -> dict[str, Any] | None:
        try:
            if self.report_path.stat().st_size > 64 * 1024:
                return {"status": "invalid", "message": "watchdog report exceeded size limit"}
            payload = json.loads(self.report_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _runtime_snapshot(station_id: int) -> dict[str, Any]:
        try:
            from app.api.runtime import _runtime_status_payload

            payload = dict(_runtime_status_payload(int(station_id)) or {})
            worker = dict(payload.get("station_worker") or payload.get("worker_loop") or {})
            mount = dict(payload.get("icecast_mount_health") or {})
            return {
                "running": bool(payload.get("running")),
                "worker_running": bool(worker.get("running")),
                "program_running": bool(payload.get("program_running")),
                "input_present": bool(payload.get("active_input_uri")),
                "output_running": bool(payload.get("icecast_sink_running")),
                "mount_healthy": mount.get("mount_healthy"),
                "encoder_error_count": int(mount.get("encoder_error_count") or 0),
            }
        except Exception as exc:
            _log.warning("Watchdog runtime snapshot failed station=%s: %s", station_id, exc)
            return {"running": False, "worker_running": False, "error": "runtime_status_failed"}

    def snapshot(self) -> dict[str, Any]:
        init_db()
        conn = get_connection()
        try:
            settings: dict[int, dict[str, str]] = {}
            for row in conn.execute(
                "SELECT station_id,key,value FROM station_settings WHERE station_id IN (1,4,8,9)"
            ).fetchall():
                settings.setdefault(int(row["station_id"]), {})[str(row["key"])] = str(
                    row["value"] or ""
                )
            latest = conn.execute("SELECT id FROM broadcast_campaigns ORDER BY id DESC LIMIT 1").fetchone()
            campaign_id = int(latest["id"]) if latest is not None else 0
            profile_rows = conn.execute(
                "SELECT station_id,genre,managed_folder FROM broadcast_campaign_stations "
                "WHERE campaign_id=? ORDER BY station_id",
                (campaign_id,),
            ).fetchall() if campaign_id else []
            profiles = []
            for row in profile_rows:
                station_id = int(row["station_id"])
                station_settings = settings.get(station_id, {})
                expected_folder = str(row["managed_folder"] or "")
                actual_folder = station_settings.get("music_library_folder", "")
                active_tracks = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM tracks WHERE station_id=? AND is_active=1 "
                        "AND lower(track_type)='music'",
                        (station_id,),
                    ).fetchone()[0]
                )
                pending_items = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM queue_items WHERE station_id=? AND status='pending'",
                        (station_id,),
                    ).fetchone()[0]
                )
                try:
                    folder_exists = Path(expected_folder).is_dir()
                except OSError:
                    folder_exists = False
                folder_matches = _same_windows_path(actual_folder, expected_folder)
                replace_mode = station_settings.get("library_management_mode", "").casefold() == "replace"
                interval_ok = station_settings.get("library_rescan_interval_seconds", "") == "600"
                recursive_ok = _truthy(
                    station_settings.get("library_recursive", "false")
                )
                profile_ok = bool(
                    folder_exists
                    and folder_matches
                    and replace_mode
                    and interval_ok
                    and recursive_ok
                    and active_tracks > 0
                )
                profiles.append(
                    {
                        "station_id": station_id,
                        "genre": str(row["genre"] or ""),
                        "folder_matches": folder_matches,
                        "folder_exists": folder_exists,
                        "replace_mode": replace_mode,
                        "rescan_interval_seconds": int(
                            float(station_settings.get("library_rescan_interval_seconds", "0") or 0)
                        ),
                        "recursive": _truthy(station_settings.get("library_recursive", "false")),
                        "active_tracks": active_tracks,
                        "pending_items": pending_items,
                        "ok": profile_ok,
                    }
                )
        finally:
            conn.close()

        stations = []
        for station_id, (genre, stream_url) in WATCHDOG_STATIONS.items():
            stations.append(
                {
                    "station_id": station_id,
                    "genre": genre,
                    "stream_url": stream_url,
                    "runtime": self._runtime_snapshot(station_id),
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stations": stations,
            "managed_profiles": profiles,
            "managed_profiles_ok": len(profiles) == len(CAMPAIGN_STATION_IDS)
            and all(bool(item["ok"]) for item in profiles),
            "last_run": self._last_report(),
        }

    @staticmethod
    def _sync_current_profile(station_id: int) -> dict[str, Any]:
        from app.api.legacy import LibraryFolderSyncPayload, sync_station_library_folder

        conn = get_connection()
        try:
            values = {
                str(row["key"]): str(row["value"] or "")
                for row in conn.execute(
                    "SELECT key,value FROM station_settings WHERE station_id=?",
                    (int(station_id),),
                ).fetchall()
            }
        finally:
            conn.close()
        return sync_station_library_folder(
            LibraryFolderSyncPayload(
                station_id=int(station_id),
                folder=values.get("music_library_folder", ""),
                recursive=_truthy(values.get("library_recursive", "false")),
                track_type="music",
                mode=values.get("library_management_mode", "replace"),
                profile_label=values.get("library_profile_label", ""),
                default_genre=values.get("library_default_genre", ""),
                default_language=values.get("library_default_language", ""),
                skip_unplayable=_truthy(values.get("library_skip_unplayable", "true"), True),
                incremental=True,
                guard_configured_folder=True,
            )
        )

    def repair(
        self,
        *,
        station_ids: list[int],
        repair_managed_profiles: bool,
    ) -> dict[str, Any]:
        selected = sorted({int(item) for item in station_ids})
        invalid = sorted(set(selected) - set(WATCHDOG_STATIONS))
        if invalid:
            raise ValueError("invalid_watchdog_station_ids")
        profile_results = []
        if repair_managed_profiles:
            conn = get_connection()
            try:
                enforced = BroadcastCampaignService(conn).ensure_managed_profiles()
            finally:
                conn.close()
            for station_id in enforced:
                profile_results.append(
                    {"station_id": station_id, "result": self._sync_current_profile(station_id)}
                )

        restarted = []
        deferred = []
        errors = []
        if selected:
            from app.api.runtime import (
                RuntimeLoopStartPayload,
                operator_start_runtime_loop,
                operator_stop_runtime,
            )

            for station_id in selected:
                runtime = self._runtime_snapshot(station_id)
                if (
                    runtime.get("running")
                    and runtime.get("worker_running")
                    and runtime.get("program_running")
                    and runtime.get("output_running")
                    and runtime.get("mount_healthy") is True
                ):
                    deferred.append(
                        {
                            "station_id": station_id,
                            "reason": "public_probe_disagreed_with_healthy_source",
                        }
                    )
                    continue
                try:
                    operator_stop_runtime(station_id)
                    status = operator_start_runtime_loop(station_id, RuntimeLoopStartPayload())
                    restarted.append(
                        {
                            "station_id": station_id,
                            "running": bool(status.get("running")),
                            "worker_running": bool(status.get("worker_running")),
                        }
                    )
                except Exception as exc:
                    _log.exception("Watchdog station repair failed station=%s", station_id)
                    errors.append({"station_id": station_id, "error": str(exc)[:300]})
        return {
            "ok": not errors,
            "restarted": restarted,
            "deferred": deferred,
            "managed_profile_repairs": profile_results,
            "errors": errors,
            "snapshot": self.snapshot(),
        }

    def record_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "status": str(payload.get("status") or "unknown")[:40],
            "message": str(payload.get("message") or "")[:500],
            "failed_station_ids": sorted(
                {
                    int(item)
                    for item in payload.get("failed_station_ids") or []
                    if int(item) in WATCHDOG_STATIONS
                }
            ),
            "managed_profiles_ok": bool(payload.get("managed_profiles_ok")),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(allowed, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.report_path)
        return allowed


audio_watchdog_service = AudioWatchdogService()
