from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_public_base_url
from app.db import get_connection, init_db
from app.repositories.settings_repo import SettingsRepository
from app.repositories.station_output_repo import StationOutputRepository
from app.security.credential_vault import resolve_credential_value, store_system_secret
from app.services.audit_chain import audit_chain
from app.services.audio_stream_probe import probe_configured_audio
from app.services.ha_coordinator import ha_coordinator
from app.services.replication_journal import canonical_json, replication_journal

_PROFILES = {
    "opus_32": {"bitrate": 32, "label": "Opus Low — 32 kbps"},
    "opus_64": {"bitrate": 64, "label": "Opus 64 kbps (legacy)"},
    "opus_96": {"bitrate": 96, "label": "Opus 96 kbps (legacy)"},
    "opus_192": {"bitrate": 192, "label": "Opus Normal — 192 kbps"},
    "mp3_128": {"bitrate": 128, "label": "Most compatible - MP3 128 kbps"},
    "aac_lc_196": {"bitrate": 196, "label": "AAC-LC 196 kbps"},
    # Read old saved drafts without claiming the native FFmpeg encoder is HE-AAC.
    "aac_plus_196": {"bitrate": 196, "label": "AAC-LC 196 kbps (legacy name)"},
    "aac_192": {"bitrate": 192, "label": "AAC 192 kbps"},
    "opus_196": {"bitrate": 196, "label": "Opus 196 kbps"},
    "opus_320": {"bitrate": 320, "label": "Opus 320 kbps"},
}

_OUTPUT_SETTING_KEYS = (
    "output_mode",
    "speaker_monitor_enabled",
    "output_device_id",
    "icecast_host",
    "icecast_port",
    "icecast_mount",
    "icecast_username",
    "icecast_password",
    "icecast_tls_enabled",
    "output_gain_db",
    "stream_codec_profile",
    "stream_bitrate_kbps",
    "source_protocol",
)


def _hash_config(config: dict) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def _public_config(config: dict) -> dict:
    output = dict(config or {})
    output.pop("password_reference", None)
    output.pop("icecast_password", None)
    output["icecast_password_configured"] = bool(config.get("password_reference") or config.get("icecast_password_configured"))
    return output


class StreamConfigError(RuntimeError):
    pass


class StreamConfigService:
    def create_draft(self, config: dict, *, actor_id: int, allow_advanced: bool = True) -> dict:
        init_db()
        normalized = self._normalize(config)
        if not allow_advanced:
            self._enforce_basic_change(normalized)
        password = str(normalized.pop("icecast_password", "") or "")
        if password:
            normalized["password_reference"] = store_system_secret(f"stream-draft-{uuid.uuid4().hex}", password)
        else:
            # A blank password in the UI means "keep the protected password",
            # not "erase it". Carry the existing station reference into the
            # draft so apply cannot silently replace a working credential.
            conn = get_connection()
            try:
                existing = StationOutputRepository(conn).get_raw(
                    int(normalized["station_id"])
                )
                if existing is not None and str(existing["icecast_password"] or ""):
                    normalized["password_reference"] = str(
                        existing["icecast_password"]
                    )
            finally:
                conn.close()
        body = canonical_json(normalized)
        digest = _hash_config(normalized)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO stream_config_drafts(station_id, config_json, config_hash, created_by) VALUES (?, ?, ?, ?)",
                (int(normalized["station_id"]), body, digest, int(actor_id)),
            )
            conn.commit()
            draft_id = int(cur.lastrowid)
        finally:
            conn.close()
        audit_chain.append(category="stream", action="draft.created", station_id=int(normalized["station_id"]), actor_id=actor_id, payload={"draft_id": draft_id, "config": _public_config(normalized)})
        return self.get_draft(draft_id)

    @staticmethod
    def _enforce_basic_change(config: dict) -> None:
        """Basic operators may select a quality preset on the saved destination."""
        conn = get_connection()
        try:
            existing = StationOutputRepository(conn).get_raw(int(config["station_id"]))
            settings = SettingsRepository(conn).get_station(int(config["station_id"]))
        finally:
            conn.close()
        if existing is None:
            raise StreamConfigError("advanced_permission_required_for_new_destination")
        protected = {
            "local_output_enabled": bool(existing["local_output_enabled"]),
            "output_device_id": str(existing["output_device_id"] or ""),
            "icecast_enabled": bool(existing["icecast_enabled"]),
            "icecast_host": str(existing["icecast_host"] or ""),
            "icecast_port": int(existing["icecast_port"] or 0),
            "icecast_mount": str(existing["icecast_mount"] or ""),
            "icecast_user": str(existing["icecast_user"] or ""),
            "icecast_tls_enabled": str(settings.get("icecast_tls_enabled", "false")).lower() in {"1", "true", "yes", "on"},
            "output_gain_db": float(existing["output_gain_db"] or 0),
            "source_protocol": str(existing["source_protocol"] or "icecast"),
        }
        if str(config.get("icecast_password") or ""):
            raise StreamConfigError("advanced_permission_required_for_credentials")
        if any(config.get(key) != value for key, value in protected.items()):
            raise StreamConfigError("advanced_permission_required_for_destination")

    def _normalize(self, config: dict) -> dict:
        station_id = int(config.get("station_id") or 0)
        if station_id <= 0:
            raise StreamConfigError("station_id_required")
        profile = str(config.get("stream_codec_profile") or "opus_192").strip().lower()
        if profile not in _PROFILES:
            raise StreamConfigError("unsupported_stream_profile")
        host = str(config.get("icecast_host") or "").strip()
        mount = str(config.get("icecast_mount") or "").strip()
        user = str(config.get("icecast_user") or "source").strip()
        port = int(config.get("icecast_port") or 0)
        source_protocol = str(config.get("source_protocol") or "icecast").strip().lower()
        if source_protocol not in {"icecast", "shoutcast"}:
            raise StreamConfigError("unsupported_source_protocol")
        if source_protocol == "shoutcast" and not profile.startswith(("mp3_", "aac")):
            raise StreamConfigError("shoutcast_profile_must_be_mp3_or_aac")
        if not host:
            raise StreamConfigError("broadcast_address_required")
        if not 1 <= port <= 65535:
            raise StreamConfigError("invalid_stream_port")
        if source_protocol == "icecast" and (
            not mount.startswith("/")
            or ".." in mount
            or any(ch.isspace() for ch in mount)
        ):
            raise StreamConfigError("invalid_stream_mount")
        if not user:
            raise StreamConfigError("source_user_required")
        gain = float(config.get("output_gain_db") or 0)
        if not -30 <= gain <= 12:
            raise StreamConfigError("invalid_output_gain")
        return {
            "station_id": station_id,
            "local_output_enabled": bool(config.get("local_output_enabled", False)),
            "output_device_id": str(config.get("output_device_id") or "").strip(),
            "icecast_enabled": bool(config.get("icecast_enabled", True)),
            "icecast_host": host,
            "icecast_port": port,
            "icecast_mount": mount,
            "icecast_user": user,
            "icecast_password": str(config.get("icecast_password") or ""),
            "icecast_tls_enabled": bool(config.get("icecast_tls_enabled", port == 443)),
            "output_gain_db": gain,
            "stream_codec_profile": profile,
            "stream_bitrate_kbps": int(_PROFILES[profile]["bitrate"]),
            "source_protocol": source_protocol,
        }

    def get_draft(self, draft_id: int) -> dict:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM stream_config_drafts WHERE id=?", (int(draft_id),)).fetchone()
            if row is None:
                raise StreamConfigError("stream_draft_not_found")
            payload = dict(row)
            config = json.loads(str(row["config_json"]))
            payload["config"] = _public_config(config)
            payload["validation"] = json.loads(str(row["validation_json"] or "{}"))
            payload.pop("config_json", None)
            payload.pop("validation_json", None)
            return payload
        finally:
            conn.close()

    def validate(self, draft_id: int, *, timeout_seconds: float = 2.0) -> dict:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM stream_config_drafts WHERE id=?", (int(draft_id),)).fetchone()
            if row is None:
                raise StreamConfigError("stream_draft_not_found")
            config = json.loads(str(row["config_json"]))
            output_repo = StationOutputRepository(conn)
            existing = output_repo.get_raw(int(config["station_id"]))
            local_conflict = output_repo.find_active_stream_conflict(
                station_id=int(config["station_id"]),
                host=str(config["icecast_host"]),
                port=int(config["icecast_port"]),
                mount=str(config["icecast_mount"]),
                source_protocol=str(config["source_protocol"]),
            ) if bool(config.get("icecast_enabled")) else None
        finally:
            conn.close()
        checks = {
            "locally_valid": {"status": "ready", "message": "The stream settings are complete."},
            "destination_reachable": {"status": "needs_attention", "message": "The destination has not been reached."},
            "credentials_verified": {"status": "pending", "message": "No non-destructive credential proof is available yet."},
            "mount_conflict": {"status": "pending", "message": "Mount occupancy evidence is not available yet."},
            "standby_ready": {"status": "ready", "message": "Standalone mode does not require a standby."},
            "rollback_ready": {"status": "ready" if existing else "needs_attention", "message": "The previous output is saved." if existing else "This is the first output configuration."},
            "live_output_verified": {"status": "pending", "message": "Live output is verified only after safe application."},
            "required_media": {"status": "ready", "message": "Queued media paths are present on this node."},
        }
        if local_conflict is not None:
            checks["mount_conflict"] = {
                "status": "unsafe",
                "message": (
                    "This destination is already assigned to station "
                    f"{int(local_conflict['station_id'])}. Choose a unique mount."
                ),
            }
        media_conn = get_connection()
        try:
            missing_media = [
                str(row["file_path"] or "")
                for row in media_conn.execute(
                    "SELECT t.file_path FROM queue_items q JOIN tracks t ON t.id=q.track_id "
                    "WHERE q.station_id=? AND q.status IN ('playing','pending') ORDER BY q.position LIMIT 100",
                    (int(config["station_id"]),),
                ).fetchall()
                if str(row["file_path"] or "") and not Path(str(row["file_path"])).is_file()
            ]
        finally:
            media_conn.close()
        if missing_media:
            checks["required_media"] = {"status": "unsafe", "message": f"{len(missing_media)} queued media file(s) are missing on this node."}
        reachable = False
        can_read_status = False
        try:
            raw = socket.create_connection((str(config["icecast_host"]), int(config["icecast_port"])), timeout=max(0.2, float(timeout_seconds)))
            can_read_status = hasattr(raw, "getpeername")
            if bool(config.get("icecast_tls_enabled")):
                context = ssl.create_default_context()
                wrapped = context.wrap_socket(raw, server_hostname=str(config["icecast_host"]))
                wrapped.close()
            else:
                raw.close()
            reachable = True
            checks["destination_reachable"] = {"status": "ready", "message": "The destination accepted a network connection."}
        except OSError as exc:
            checks["destination_reachable"] = {"status": "needs_attention", "message": f"Destination could not be reached: {exc}"}
        if reachable and can_read_status:
            scheme = "https" if config.get("icecast_tls_enabled") else "http"
            status_url = f"{scheme}://{config['icecast_host']}:{int(config['icecast_port'])}/status-json.xsl"
            try:
                request = urllib.request.Request(status_url, headers={"User-Agent": "RadioTEDU-OnAir/HA-validation"})
                with urllib.request.urlopen(request, timeout=max(0.5, float(timeout_seconds))) as response:
                    status_payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
                sources = (status_payload.get("icestats") or {}).get("source") or []
                if isinstance(sources, dict):
                    sources = [sources]
                active_mounts = {
                    "/" + str(item.get("listenurl") or "").rsplit("/", 1)[-1]
                    for item in sources
                    if isinstance(item, dict) and item.get("listenurl")
                }
                same_live_destination = bool(
                    existing
                    and str(existing["icecast_host"] or "") == str(config["icecast_host"])
                    and int(existing["icecast_port"] or 0) == int(config["icecast_port"])
                    and str(existing["icecast_mount"] or "") == str(config["icecast_mount"])
                )
                conflict = str(config["icecast_mount"]) in active_mounts and not same_live_destination
                if local_conflict is None:
                    checks["mount_conflict"] = {
                        "status": "unsafe" if conflict else "ready",
                        "message": "The requested mount is already active under another configuration." if conflict else "No conflicting active mount was reported.",
                    }
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if local_conflict is None:
                    checks["mount_conflict"] = {"status": "pending", "message": f"Icecast did not expose non-destructive mount evidence: {exc}"}
        credential_available = bool(config.get("password_reference") or (existing and str(existing["icecast_password"] or "")))
        if credential_available:
            checks["credentials_verified"] = {"status": "pending", "message": "A protected source password is available; it is not marked verified until controlled application succeeds."}
        ha = ha_coordinator.snapshot()
        if ha["enabled"]:
            checks["standby_ready"] = {
                "status": "ready" if ha["quorum"] and ha["role"] == "leader" else "unsafe",
                "message": "HA quorum is available." if ha["quorum"] else "HA quorum is unavailable; safe application is blocked.",
            }
        unsafe = any(check["status"] == "unsafe" for check in checks.values()) or not credential_available
        status = "unsafe" if unsafe else ("ready" if reachable else "needs_attention")
        report = {"outcome": status, "checks": checks, "validated_at": datetime.now(timezone.utc).isoformat()}
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE stream_config_drafts SET status='validated', validation_json=?, revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (canonical_json(report), int(draft_id)),
            )
            conn.commit()
        finally:
            conn.close()
        return {"draft_id": int(draft_id), **report}

    @staticmethod
    def _snapshot_output(row, station_settings: dict) -> dict:
        payload = dict(row) if row is not None else {}
        payload["_station_output_present"] = row is not None
        payload["_station_settings"] = {
            key: str(station_settings[key])
            for key in _OUTPUT_SETTING_KEYS
            if key in station_settings
        }
        payload["icecast_tls_enabled"] = str(
            station_settings.get("icecast_tls_enabled", "false")
        ).lower() in {"1", "true", "yes", "on"}
        return payload

    def apply(
        self,
        draft_id: int,
        *,
        actor_id: int,
        idempotency_key: str,
        override_reason: str = "",
        defer_listener_verification: bool = False,
    ) -> dict:
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 128:
            raise StreamConfigError("idempotency_key_required")
        init_db()
        conn = get_connection()
        try:
            prior_operation = conn.execute("SELECT * FROM stream_config_operations WHERE idempotency_key=?", (key,)).fetchone()
            if prior_operation:
                return self.operation(int(prior_operation["id"]))
            row = conn.execute("SELECT * FROM stream_config_drafts WHERE id=?", (int(draft_id),)).fetchone()
            if row is None:
                raise StreamConfigError("stream_draft_not_found")
            if str(row["status"]) != "validated":
                raise StreamConfigError("stream_draft_not_validated")
            report = json.loads(str(row["validation_json"] or "{}"))
            validation_outcome = str(report.get("outcome") or "unsafe")
            may_defer = bool(
                defer_listener_verification
                and validation_outcome in {"ready", "needs_attention"}
            )
            if validation_outcome != "ready" and not (
                may_defer or str(override_reason or "").strip()
            ):
                raise StreamConfigError(
                    "stream_draft_unsafe"
                    if validation_outcome == "unsafe"
                    else "stream_draft_needs_attention"
                )
            config = json.loads(str(row["config_json"]))
            station_id = int(config["station_id"])
            settings_repo = SettingsRepository(conn)
            previous = self._snapshot_output(StationOutputRepository(conn).get_raw(station_id), settings_repo.get_station(station_id))
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO stream_config_operations(draft_id, station_id, idempotency_key, status, previous_config_json, created_by) "
                "VALUES (?, ?, ?, 'preparing', ?, ?)",
                (int(draft_id), station_id, key, canonical_json(previous), int(actor_id)),
            )
            operation_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        runtime_registry = None
        runtime_before = {"running": False}
        verification_output: dict = {}
        verification_station_settings: dict = {}
        verification_public_base_url = ""
        try:
            ha_coordinator.require_safe_mutation(override_reason=override_reason)
            journal = replication_journal.append("stream_config", draft_id, "apply", _public_config(config))
            if ha_coordinator.snapshot()["enabled"]:
                ha_coordinator.replicate_ordered(through_sequence=int(journal["sequence"]))
            try:
                from app.api.runtime import runtime_registry
                runtime_before = runtime_registry.status(station_id)
            except Exception:
                runtime_registry = None
            repo_conn = get_connection()
            try:
                repo = StationOutputRepository(repo_conn)
                conflict = repo.find_active_stream_conflict(
                    station_id=station_id,
                    host=str(config["icecast_host"]),
                    port=int(config["icecast_port"]),
                    mount=str(config["icecast_mount"]),
                    source_protocol=str(config["source_protocol"]),
                ) if bool(config.get("icecast_enabled")) else None
                if conflict is not None:
                    raise StreamConfigError(
                        f"stream_mount_conflict_station_{int(conflict['station_id'])}"
                    )
                password = resolve_credential_value(str(config.get("password_reference") or ""))
                repo.upsert(
                    station_id=station_id,
                    local_output_enabled=bool(config["local_output_enabled"]),
                    output_device_id=str(config["output_device_id"]),
                    icecast_enabled=bool(config["icecast_enabled"]),
                    icecast_host=str(config["icecast_host"]),
                    icecast_port=int(config["icecast_port"]),
                    icecast_mount=str(config["icecast_mount"]),
                    icecast_user=str(config["icecast_user"]),
                    icecast_password=password,
                    output_gain_db=float(config["output_gain_db"]),
                    stream_codec_profile=str(config["stream_codec_profile"]),
                    stream_bitrate_kbps=int(config["stream_bitrate_kbps"]),
                    source_protocol=str(config["source_protocol"]),
                )
                settings_repository = SettingsRepository(repo_conn)
                settings_repository.upsert_station(
                    station_id,
                    {
                        "output_mode": str(config["source_protocol"]) if config["icecast_enabled"] else "speaker",
                        "source_protocol": str(config["source_protocol"]),
                        "speaker_monitor_enabled": str(bool(config["local_output_enabled"])).lower(),
                        "output_device_id": str(config["output_device_id"]),
                        "icecast_host": str(config["icecast_host"]),
                        "icecast_port": str(config["icecast_port"]),
                        "icecast_mount": str(config["icecast_mount"]),
                        "icecast_username": str(config["icecast_user"]),
                        "icecast_password": "",
                        "icecast_tls_enabled": str(bool(config["icecast_tls_enabled"])).lower(),
                        "output_gain_db": str(config["output_gain_db"]),
                        "stream_codec_profile": str(config["stream_codec_profile"]),
                        "stream_bitrate_kbps": str(config["stream_bitrate_kbps"]),
                    },
                )
                stored = repo.get_raw(station_id)
                verified = bool(stored and str(stored["icecast_host"]) == str(config["icecast_host"]) and str(stored["icecast_mount"]) == str(config["icecast_mount"]))
                verification_output = dict(stored) if stored is not None else {}
                verification_station_settings = settings_repository.get_station(
                    station_id
                )
                system_settings = settings_repository.get_system()
                verification_public_base_url = str(
                    get_public_base_url()
                    or system_settings.get("stream_public_base_url")
                    or ""
                ).strip()
            finally:
                repo_conn.close()
            if not verified:
                raise StreamConfigError("authoritative_readback_failed")
            live_verified = False
            listener_audio_verified = False
            verification_seconds = 0.0
            listener_verification_deferred = bool(
                defer_listener_verification and config["icecast_enabled"]
            )
            if (
                runtime_registry is not None
                and bool(runtime_before.get("running"))
                and runtime_before.get("active_input_uri")
            ):
                restarted = runtime_registry.start_station(
                    station_id,
                    str(runtime_before["active_input_uri"]),
                    stream_title=str(runtime_before.get("stream_title") or ""),
                    stream_artist=str(runtime_before.get("stream_artist") or ""),
                    track_type=str(runtime_before.get("track_type") or "music"),
                )
                if not bool(
                    restarted.get("running")
                    and restarted.get("output_feed_active")
                ):
                    raise StreamConfigError("live_output_readback_failed")
                if not listener_verification_deferred:
                    verification_seconds = max(
                        1.0,
                        min(60.0, float(os.getenv("CLEANROOM_STREAM_VERIFY_SECONDS", "60") or 60)),
                    )
                    deadline = time.monotonic() + verification_seconds
                    listener_successes = 0
                    consecutive_listener_failures = 0
                    last_listener_ok = not bool(config["icecast_enabled"])
                    while time.monotonic() < deadline:
                        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                        observed = runtime_registry.status(station_id)
                        if not bool(observed.get("running") and observed.get("output_feed_active")):
                            raise StreamConfigError("live_output_verification_window_failed")
                        if bool(config["icecast_enabled"]):
                            listener_probe = probe_configured_audio(
                                verification_output,
                                verification_station_settings,
                                verification_public_base_url,
                                timeout=min(2.0, max(0.2, verification_seconds)),
                            )
                            last_listener_ok = bool(listener_probe.ok)
                            if last_listener_ok:
                                listener_successes += 1
                                consecutive_listener_failures = 0
                            else:
                                consecutive_listener_failures += 1
                                if consecutive_listener_failures >= 2:
                                    raise StreamConfigError(
                                        "listener_audio_verification_failed"
                                    )
                    required_listener_successes = (
                        2 if verification_seconds >= 2.0 else 1
                    )
                    listener_audio_verified = bool(
                        not config["icecast_enabled"]
                        or (
                            last_listener_ok
                            and listener_successes >= required_listener_successes
                        )
                    )
                    if not listener_audio_verified:
                        raise StreamConfigError("listener_audio_verification_failed")
                    live_verified = True
            result = {
                "outcome": (
                    "needs_attention"
                    if listener_verification_deferred
                    else "ready"
                ),
                "message": (
                    "The protected configuration was applied, read back, and "
                    "verified at the listener."
                    if live_verified
                    else (
                        "The protected configuration was saved and read back. "
                        "The broadcast engine is retrying the listener destination "
                        "in the background."
                        if listener_verification_deferred
                        else "The protected configuration was saved and read back; "
                        "listener verification is deferred until the station starts."
                    )
                ),
                "journal": journal,
                "live_output_verified": live_verified,
                "listener_audio_verified": listener_audio_verified,
                "listener_verification_deferred": listener_verification_deferred,
                "validation_outcome_at_apply": validation_outcome,
                "verification_window_seconds": (
                    verification_seconds if live_verified else 0
                ),
            }
            conn = get_connection()
            try:
                conn.execute("UPDATE stream_config_operations SET status='applied', result_json=?, completed_at=CURRENT_TIMESTAMP WHERE id=?", (canonical_json(result), operation_id))
                conn.execute("UPDATE stream_config_drafts SET status='applied', updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(draft_id),))
                conn.commit()
            finally:
                conn.close()
            audit_chain.append(category="stream", action="configuration.applied", station_id=station_id, actor_id=actor_id, payload={"operation_id": operation_id, "draft_id": draft_id, "config": _public_config(config)})
            return self.operation(operation_id)
        except Exception as exc:
            self._restore_previous(operation_id)
            try:
                if runtime_registry is not None and bool(runtime_before.get("running")) and runtime_before.get("active_input_uri"):
                    runtime_registry.start_station(
                        station_id,
                        str(runtime_before["active_input_uri"]),
                        stream_title=str(runtime_before.get("stream_title") or ""),
                        stream_artist=str(runtime_before.get("stream_artist") or ""),
                        track_type=str(runtime_before.get("track_type") or "music"),
                    )
            except Exception:
                pass
            conn = get_connection()
            try:
                conn.execute("UPDATE stream_config_operations SET status='rolled_back', result_json=?, completed_at=CURRENT_TIMESTAMP WHERE id=?", (canonical_json({"outcome": "unsafe", "error": str(exc), "message": "The previous output was restored."}), operation_id))
                conn.commit()
            finally:
                conn.close()
            audit_chain.append(category="stream", action="configuration.auto_rolled_back", station_id=station_id, actor_id=actor_id, payload={"operation_id": operation_id, "error": str(exc)})
            raise

    def _restore_previous(self, operation_id: int) -> None:
        conn = get_connection()
        try:
            operation = conn.execute("SELECT * FROM stream_config_operations WHERE id=?", (int(operation_id),)).fetchone()
            if operation is None:
                raise StreamConfigError("stream_operation_not_found")
            previous = json.loads(str(operation["previous_config_json"] or "{}"))
            if not previous:
                return
            station_id = int(operation["station_id"])
            output_was_present = bool(
                previous.get(
                    "_station_output_present",
                    bool(previous.get("icecast_host")),
                )
            )
            if output_was_present:
                StationOutputRepository(conn).upsert(
                    station_id=station_id,
                    local_output_enabled=bool(previous.get("local_output_enabled")),
                    output_device_id=str(previous.get("output_device_id") or ""),
                    icecast_enabled=bool(previous.get("icecast_enabled")),
                    icecast_host=str(previous.get("icecast_host") or "127.0.0.1"),
                    icecast_port=int(previous.get("icecast_port") or 8000),
                    icecast_mount=str(previous.get("icecast_mount") or "/stream"),
                    icecast_user=str(previous.get("icecast_user") or "source"),
                    icecast_password=str(previous.get("icecast_password") or ""),
                    output_gain_db=float(previous.get("output_gain_db") or 0),
                    stream_codec_profile=str(previous.get("stream_codec_profile") or "opus_192"),
                    stream_bitrate_kbps=int(previous.get("stream_bitrate_kbps") or 192),
                    source_protocol=str(previous.get("source_protocol") or "icecast"),
                )
            else:
                conn.execute(
                    "DELETE FROM station_outputs WHERE station_id=?",
                    (station_id,),
                )
                conn.commit()

            settings_snapshot = previous.get("_station_settings")
            if isinstance(settings_snapshot, dict):
                placeholders = ",".join("?" for _ in _OUTPUT_SETTING_KEYS)
                conn.execute(
                    "DELETE FROM station_settings WHERE station_id=? "
                    f"AND key IN ({placeholders})",
                    (station_id, *_OUTPUT_SETTING_KEYS),
                )
                conn.commit()
                SettingsRepository(conn).upsert_station(
                    station_id,
                    {
                        str(key): str(value)
                        for key, value in settings_snapshot.items()
                        if key in _OUTPUT_SETTING_KEYS
                    },
                )
            else:
                SettingsRepository(conn).upsert_station(
                    station_id,
                    {
                        "icecast_tls_enabled": str(
                            bool(previous.get("icecast_tls_enabled"))
                        ).lower()
                    },
                )
        finally:
            conn.close()

    def rollback(self, operation_id: int, *, actor_id: int) -> dict:
        self._restore_previous(operation_id)
        conn = get_connection()
        try:
            conn.execute("UPDATE stream_config_operations SET status='rolled_back', completed_at=CURRENT_TIMESTAMP WHERE id=?", (int(operation_id),))
            conn.commit()
        finally:
            conn.close()
        audit_chain.append(category="stream", action="configuration.manual_rollback", actor_id=actor_id, payload={"operation_id": int(operation_id)})
        return self.operation(operation_id)

    def operation(self, operation_id: int) -> dict:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM stream_config_operations WHERE id=?", (int(operation_id),)).fetchone()
            if row is None:
                raise StreamConfigError("stream_operation_not_found")
            payload = dict(row)
            payload["result"] = json.loads(str(row["result_json"] or "{}"))
            payload.pop("result_json", None)
            payload.pop("previous_config_json", None)
            return payload
        finally:
            conn.close()


stream_config_service = StreamConfigService()
