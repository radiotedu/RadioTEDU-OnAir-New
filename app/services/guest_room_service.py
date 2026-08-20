from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.db import get_connection, init_db
from app.services.audit_chain import audit_chain


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class GuestRoomError(RuntimeError):
    pass


class GuestRoomService:
    max_admitted_guests = 4

    @staticmethod
    def _invalidate_audio_controls() -> None:
        try:
            from app.audio.guest_audio_registry import guest_audio_registry
            guest_audio_registry.invalidate_controls()
        except Exception:
            pass

    @staticmethod
    def _replicate_snapshot(studio_id: int) -> None:
        """Replicate token hashes and safe control state, never invite secrets."""
        try:
            from app.services.ha_coordinator import ha_coordinator
            from app.services.replication_journal import replication_journal

            if not ha_coordinator.snapshot()["enabled"]:
                return
            conn = get_connection()
            try:
                payload = {
                    "studio_id": int(studio_id),
                    "invites": [dict(row) for row in conn.execute("SELECT * FROM guest_invites WHERE studio_id=?", (int(studio_id),)).fetchall()],
                    "sessions": [dict(row) for row in conn.execute("SELECT * FROM guest_sessions WHERE studio_id=?", (int(studio_id),)).fetchall()],
                }
            finally:
                conn.close()
            journal = replication_journal.append("guest_room", int(studio_id), "snapshot", payload)
            ha_coordinator.replicate_ordered(through_sequence=int(journal["sequence"]))
        except Exception as exc:
            audit_chain.append(
                category="guest",
                action="replication.degraded",
                payload={"studio_id": int(studio_id), "error": str(exc)},
            )

    def create_invite(self, studio_id: int, *, actor_id: int, base_url: str = "") -> dict:
        init_db()
        token = secrets.token_urlsafe(32)
        expires = _utc_now() + timedelta(hours=2)
        conn = get_connection()
        try:
            studio = conn.execute("SELECT * FROM studios WHERE id=? AND is_active=1", (int(studio_id),)).fetchone()
            if studio is None:
                raise GuestRoomError("studio_not_found")
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO guest_invites(studio_id, station_id, token_hash, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
                (int(studio_id), int(studio["station_id"]), _hash_token(token), int(actor_id), _iso(expires)),
            )
            conn.commit()
            invite_id = int(cur.lastrowid)
            station_id = int(studio["station_id"])
        finally:
            conn.close()
        audit_chain.append(category="guest", action="invite.created", station_id=station_id, actor_id=actor_id, payload={"invite_id": invite_id, "studio_id": int(studio_id), "expires_at": _iso(expires)})
        self._replicate_snapshot(studio_id)
        root = str(base_url or "").rstrip("/")
        return {"id": invite_id, "expires_at": _iso(expires), "join_url": f"{root}/guest.html#invite={token}"}

    def revoke_invite(self, studio_id: int, invite_id: int, *, actor_id: int) -> None:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_invites WHERE id=? AND studio_id=?", (int(invite_id), int(studio_id))).fetchone()
            if row is None:
                raise GuestRoomError("guest_invite_not_found")
            conn.execute("UPDATE guest_invites SET revoked_at=CURRENT_TIMESTAMP WHERE id=?", (int(invite_id),))
            conn.commit()
            station_id = int(row["station_id"])
        finally:
            conn.close()
        audit_chain.append(category="guest", action="invite.revoked", station_id=station_id, actor_id=actor_id, payload={"invite_id": int(invite_id)})
        self._replicate_snapshot(studio_id)

    def redeem(self, token: str, display_name: str) -> dict:
        normalized_name = " ".join(str(display_name or "").strip().split())
        if not 1 <= len(normalized_name) <= 80:
            raise GuestRoomError("guest_display_name_required")
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_invites WHERE token_hash=?", (_hash_token(token),)).fetchone()
            if row is None:
                raise GuestRoomError("guest_invite_invalid")
            if row["revoked_at"] is not None or row["redeemed_at"] is not None:
                raise GuestRoomError("guest_invite_unavailable")
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if expires <= _utc_now():
                raise GuestRoomError("guest_invite_expired")
            session_token = secrets.token_urlsafe(32)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO guest_sessions(invite_id, studio_id, station_id, display_name, session_token_hash) VALUES (?, ?, ?, ?, ?)",
                (int(row["id"]), int(row["studio_id"]), int(row["station_id"]), normalized_name, _hash_token(session_token)),
            )
            session_id = int(cur.lastrowid)
            conn.execute("UPDATE guest_invites SET redeemed_at=CURRENT_TIMESTAMP WHERE id=?", (int(row["id"]),))
            conn.commit()
            station_id = int(row["station_id"])
            studio_id = int(row["studio_id"])
        finally:
            conn.close()
        audit_chain.append(category="guest", action="invite.redeemed", station_id=station_id, payload={"session_id": session_id, "studio_id": studio_id, "display_name": normalized_name})
        self._replicate_snapshot(studio_id)
        return {"session_id": session_id, "session_token": session_token, "studio_id": studio_id, "station_id": station_id, "display_name": normalized_name, "status": "lobby"}

    def authenticate_session(self, session_token: str) -> dict:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_sessions WHERE session_token_hash=?", (_hash_token(session_token),)).fetchone()
            if row is None or str(row["status"]) in {"rejected", "kicked", "left"}:
                raise GuestRoomError("guest_session_invalid")
            return dict(row)
        finally:
            conn.close()

    def set_connected(self, session_id: int, connected: bool, quality: str = "unknown") -> dict:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE guest_sessions SET is_connected=?, connection_quality=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(bool(connected)), str(quality or "unknown")[:32], int(session_id)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM guest_sessions WHERE id=?", (int(session_id),)).fetchone()
            result = dict(row) if row else {}
        finally:
            conn.close()
        self._invalidate_audio_controls()
        if result.get("studio_id"):
            self._replicate_snapshot(int(result["studio_id"]))
        return result

    def snapshot(self, studio_id: int) -> dict:
        init_db()
        conn = get_connection()
        try:
            studio = conn.execute("SELECT * FROM studios WHERE id=?", (int(studio_id),)).fetchone()
            if studio is None:
                raise GuestRoomError("studio_not_found")
            sessions = [dict(row) for row in conn.execute("SELECT * FROM guest_sessions WHERE studio_id=? AND status NOT IN ('left') ORDER BY id", (int(studio_id),)).fetchall()]
            invites = [
                {key: row[key] for key in ("id", "expires_at", "redeemed_at", "revoked_at", "created_at")}
                for row in conn.execute("SELECT * FROM guest_invites WHERE studio_id=? ORDER BY id DESC LIMIT 25", (int(studio_id),)).fetchall()
            ]
            recording = conn.execute("SELECT * FROM guest_recordings WHERE studio_id=? AND status IN ('pending_consent','recording') ORDER BY id DESC LIMIT 1", (int(studio_id),)).fetchone()
            try:
                from app.audio.guest_audio_registry import guest_audio_registry
                levels = {int(item["session_id"]): item for item in guest_audio_registry.snapshots(int(studio["station_id"]))}
                for session in sessions:
                    level = levels.get(int(session["id"]), {})
                    session["level_db"] = float(level.get("level_db", -60.0))
                    session["peak_db"] = float(level.get("peak_db", -60.0))
            except Exception:
                pass
            return {
                "studio_id": int(studio_id),
                "station_id": int(studio["station_id"]),
                "capacity": self.max_admitted_guests,
                "sessions": sessions,
                "invites": invites,
                "recording": dict(recording) if recording else None,
            }
        finally:
            conn.close()

    def admit(self, studio_id: int, session_id: int, *, actor_id: int) -> dict:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_sessions WHERE id=? AND studio_id=?", (int(session_id), int(studio_id))).fetchone()
            if row is None or str(row["status"]) != "lobby":
                raise GuestRoomError("guest_session_not_in_lobby")
            count = int(conn.execute("SELECT COUNT(*) FROM guest_sessions WHERE studio_id=? AND status='admitted'", (int(studio_id),)).fetchone()[0])
            if count >= self.max_admitted_guests:
                raise GuestRoomError("guest_room_full")
            active_recording = conn.execute("SELECT id FROM guest_recordings WHERE studio_id=? AND status='recording' LIMIT 1", (int(studio_id),)).fetchone()
            if active_recording:
                consent = conn.execute("SELECT decision FROM guest_recording_consents WHERE recording_id=? AND session_id=?", (int(active_recording["id"]), int(session_id))).fetchone()
                if consent is None or str(consent["decision"]) != "accepted":
                    raise GuestRoomError("recording_consent_required")
            conn.execute("UPDATE guest_sessions SET status='admitted', admitted_at=CURRENT_TIMESTAMP, is_on_air=0 WHERE id=?", (int(session_id),))
            conn.commit()
            updated = dict(conn.execute("SELECT * FROM guest_sessions WHERE id=?", (int(session_id),)).fetchone())
        finally:
            conn.close()
        audit_chain.append(category="guest", action="session.admitted", station_id=int(updated["station_id"]), actor_id=actor_id, payload={"session_id": int(session_id), "studio_id": int(studio_id)})
        self._invalidate_audio_controls()
        self._replicate_snapshot(studio_id)
        return updated

    def reject_or_kick(self, studio_id: int, session_id: int, *, actor_id: int, action: str) -> dict:
        normalized = "rejected" if action == "reject" else "kicked"
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_sessions WHERE id=? AND studio_id=?", (int(session_id), int(studio_id))).fetchone()
            if row is None:
                raise GuestRoomError("guest_session_not_found")
            conn.execute("UPDATE guest_sessions SET status=?, is_on_air=0, left_at=CURRENT_TIMESTAMP WHERE id=?", (normalized, int(session_id)))
            conn.commit()
            station_id = int(row["station_id"])
        finally:
            conn.close()
        audit_chain.append(category="guest", action=f"session.{normalized}", station_id=station_id, actor_id=actor_id, payload={"session_id": int(session_id), "studio_id": int(studio_id)})
        self._invalidate_audio_controls()
        self._replicate_snapshot(studio_id)
        return {"session_id": int(session_id), "status": normalized}

    def update_audio(self, studio_id: int, session_id: int, *, muted: bool | None, on_air: bool | None, gain_db: float | None, actor_id: int) -> dict:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM guest_sessions WHERE id=? AND studio_id=?", (int(session_id), int(studio_id))).fetchone()
            if row is None or str(row["status"]) != "admitted":
                raise GuestRoomError("guest_session_not_admitted")
            updates = []
            params = []
            if muted is not None:
                updates.append("is_muted=?")
                params.append(int(bool(muted)))
            if on_air is not None:
                updates.append("is_on_air=?")
                params.append(int(bool(on_air)))
            if gain_db is not None:
                gain = float(gain_db)
                if not -24 <= gain <= 12:
                    raise GuestRoomError("invalid_guest_gain")
                updates.append("gain_db=?")
                params.append(gain)
            if updates:
                params.append(int(session_id))
                conn.execute(f"UPDATE guest_sessions SET {', '.join(updates)}, last_seen_at=CURRENT_TIMESTAMP WHERE id=?", tuple(params))
                conn.commit()
            updated = dict(conn.execute("SELECT * FROM guest_sessions WHERE id=?", (int(session_id),)).fetchone())
        finally:
            conn.close()
        audit_chain.append(category="guest", action="audio.updated", station_id=int(updated["station_id"]), actor_id=actor_id, payload={"session_id": int(session_id), "muted": bool(updated["is_muted"]), "on_air": bool(updated["is_on_air"]), "gain_db": float(updated["gain_db"])})
        self._invalidate_audio_controls()
        self._replicate_snapshot(studio_id)
        return updated

    def all_off_air(self, studio_id: int, *, actor_id: int) -> dict:
        conn = get_connection()
        try:
            studio = conn.execute("SELECT station_id FROM studios WHERE id=?", (int(studio_id),)).fetchone()
            if studio is None:
                raise GuestRoomError("studio_not_found")
            cur = conn.execute("UPDATE guest_sessions SET is_on_air=0 WHERE studio_id=? AND status='admitted'", (int(studio_id),))
            conn.commit()
            count = int(cur.rowcount)
            station_id = int(studio["station_id"])
        finally:
            conn.close()
        audit_chain.append(category="guest", action="audio.all_off_air", station_id=station_id, actor_id=actor_id, payload={"studio_id": int(studio_id), "count": count})
        self._invalidate_audio_controls()
        self._replicate_snapshot(studio_id)
        return {"ok": True, "count": count}

    def guest_self_mute(self, session_token: str, muted: bool) -> dict:
        session = self.authenticate_session(session_token)
        conn = get_connection()
        try:
            conn.execute("UPDATE guest_sessions SET is_muted=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?", (int(bool(muted)), int(session["id"])))
            conn.commit()
        finally:
            conn.close()
        self._invalidate_audio_controls()
        self._replicate_snapshot(int(session["studio_id"]))
        return {"session_id": int(session["id"]), "muted": bool(muted)}


guest_room_service = GuestRoomService()
