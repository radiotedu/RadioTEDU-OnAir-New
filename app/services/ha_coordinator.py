from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from app.db import get_connection, init_db
from app.services.audit_chain import audit_chain


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def ha_token(at_time: float | None = None) -> str:
    secret = os.getenv("CLEANROOM_HA_SHARED_SECRET", "").encode("utf-8")
    if not secret:
        return ""
    bucket = int((time.time() if at_time is None else float(at_time)) // 30)
    return hmac.new(secret, f"onair-ha-v1|{bucket}".encode("utf-8"), hashlib.sha256).hexdigest()


def validate_ha_token(value: str) -> bool:
    supplied = str(value or "")
    if not supplied:
        return False
    now = time.time()
    # Accept one previous 30-second bucket for normal clock/network jitter.
    return any(hmac.compare_digest(ha_token(now - offset), supplied) for offset in (0, 30))


@dataclass
class HaSnapshot:
    enabled: bool
    node_id: str
    role: str
    term: int
    leader_id: str
    lease_expires_at: float
    quorum: bool
    peer_acks: int
    last_error: str

    def as_dict(self) -> dict:
        # JSON forbids Infinity. Standalone mode does not need a lease, so
        # expose zero rather than the internal unbounded sentinel.
        lease_expires_at = float(self.lease_expires_at) if self.enabled else 0.0
        remaining = max(0.0, lease_expires_at - time.time()) if self.enabled else 0.0
        return {
            "enabled": self.enabled,
            "node_id": self.node_id,
            "role": self.role,
            "term": self.term,
            "leader_id": self.leader_id,
            "lease_expires_at": lease_expires_at,
            "lease_remaining_seconds": round(remaining, 3),
            "quorum": self.quorum,
            "peer_acks": self.peer_acks,
            "last_error": self.last_error,
            "safe_to_broadcast": (not self.enabled) or (self.role == "leader" and self.quorum and remaining > 0),
        }


class HaCoordinator:
    """Small, fail-silent three-voter coordinator for a two-node + witness deployment."""

    def __init__(self, transport: Callable[[str, dict], dict] | None = None):
        self.enabled = _truthy("CLEANROOM_HA_ENABLED")
        self.witness_only = _truthy("CLEANROOM_HA_WITNESS_ONLY")
        self.node_id = os.getenv("CLEANROOM_HA_NODE_ID", "node-local").strip() or "node-local"
        self.peers = [p.rstrip("/") for p in os.getenv("CLEANROOM_HA_PEERS", "").split(",") if p.strip()]
        self.lease_seconds = max(3.0, float(os.getenv("CLEANROOM_HA_LEASE_SECONDS", "6")))
        self.heartbeat_seconds = max(0.25, float(os.getenv("CLEANROOM_HA_HEARTBEAT_SECONDS", "1")))
        self._transport = transport or self._http_post
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._callbacks: list[Callable[[str, dict], None]] = []
        self._role = "standalone" if not self.enabled else "follower"
        self._term = 0
        self._leader_id = self.node_id if not self.enabled else ""
        self._lease_expires_at = float("inf") if not self.enabled else 0.0
        self._lease_deadline_monotonic = 0.0
        self._quorum = not self.enabled
        self._peer_acks = 0
        self._last_error = ""
        self._last_anchor_at = 0.0

    def register_role_callback(self, callback: Callable[[str, dict], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unregister_role_callback(self, callback: Callable[[str, dict], None]) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def _load_persistent(self) -> None:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM ha_state WHERE id=1").fetchone()
            if row:
                self._term = int(row["current_term"] or 0)
                self._leader_id = str(row["leader_id"] or "")
                self._lease_expires_at = float(row["leader_lease_expires_at"] or 0)
        finally:
            conn.close()

    def _persist(self, *, voted_for: str | None = None) -> None:
        init_db()
        conn = get_connection()
        try:
            if voted_for is None:
                conn.execute(
                    "UPDATE ha_state SET current_term=?, leader_id=?, leader_lease_expires_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
                    (self._term, self._leader_id, self._lease_expires_at),
                )
            else:
                conn.execute(
                    "UPDATE ha_state SET current_term=?, voted_for=?, leader_id=?, leader_lease_expires_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
                    (self._term, voted_for, self._leader_id, self._lease_expires_at),
                )
            conn.commit()
        finally:
            conn.close()

    def start(self) -> None:
        if not self.enabled or self.witness_only or self._thread is not None:
            return
        self._load_persistent()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="onair-ha-coordinator", daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="onair-ha-lease-watchdog", daemon=True)
        self._thread.start()
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.heartbeat_seconds * 3))
        watchdog = self._watchdog_thread
        if watchdog is not None:
            watchdog.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        self._thread = None
        self._watchdog_thread = None
        if self.enabled:
            self._set_role("follower", quorum=False, leader_id="")

    def _http_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-OnAir-HA-Token": ha_token()},
        )
        with urllib.request.urlopen(request, timeout=max(1.0, self.heartbeat_seconds)) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_all(self, path: str, payload: dict) -> int:
        acknowledgements = 0
        errors = []
        for peer in self.peers:
            try:
                result = self._transport(f"{peer}{path}", payload)
                if bool(result.get("accepted")):
                    acknowledgements += 1
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(f"{peer}: {exc}")
        with self._lock:
            self._last_error = "; ".join(errors)[:500]
        return acknowledgements

    def _set_role(self, role: str, *, quorum: bool, leader_id: str) -> None:
        with self._lock:
            changed = role != self._role
            self._role = role
            self._quorum = bool(quorum)
            self._leader_id = str(leader_id or "")
            if role != "leader":
                self._lease_deadline_monotonic = 0.0
            snapshot = self.snapshot()
        if changed:
            self._configure_floating_ip(claim=role == "leader")
            audit_chain.append(category="failover", action=f"role.{role}", payload=snapshot)
            for callback in tuple(self._callbacks):
                try:
                    callback(role, snapshot)
                except Exception:
                    pass

    def _configure_floating_ip(self, *, claim: bool) -> None:
        address = os.getenv("CLEANROOM_HA_FLOATING_IP", "").strip()
        interface = os.getenv("CLEANROOM_HA_INTERFACE", "").strip()
        if os.name != "nt" or not address or not interface:
            return
        action = "add" if claim else "delete"
        cmd = ["netsh", "interface", "ipv4", action, "address", f'name={interface}', f'address={address}']
        if claim:
            cmd.extend([f'mask={os.getenv("CLEANROOM_HA_FLOATING_NETMASK", "255.255.255.0")}', "store=active"])
        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=5, creationflags=0x08000000)
        except (OSError, subprocess.SubprocessError):
            pass

    def _run(self) -> None:
        next_election = time.monotonic() + random.uniform(self.lease_seconds, self.lease_seconds * 1.5)
        while not self._stop.wait(self.heartbeat_seconds):
            now = time.time()
            if self._role == "leader":
                proposed_expiry = now + self.lease_seconds
                acks = self._post_all(
                    "/api/ha/internal/heartbeat",
                    {"term": self._term, "leader_id": self.node_id, "lease_expires_at": proposed_expiry},
                )
                with self._lock:
                    self._peer_acks = acks
                    still_leader = self._role == "leader"
                if not still_leader:
                    # The independent watchdog may have fenced us while the
                    # network call was blocked. A late ACK cannot resurrect
                    # leadership or extend the lease.
                    continue
                if acks >= 1:
                    with self._lock:
                        self._lease_expires_at = proposed_expiry
                        self._lease_deadline_monotonic = time.monotonic() + self.lease_seconds
                        self._quorum = True
                        self._leader_id = self.node_id
                    self._persist()
                    try:
                        self.replicate_ordered(limit=50)
                    except Exception as exc:
                        with self._lock:
                            self._last_error = f"journal_retry: {exc}"[:500]
                    if now - self._last_anchor_at >= 60:
                        self._anchor_audit_head()
                elif now >= self._lease_expires_at:
                    self._set_role("follower", quorum=False, leader_id="")
                    next_election = time.monotonic() + random.uniform(self.lease_seconds, self.lease_seconds * 1.5)
                continue
            if now < self._lease_expires_at:
                continue
            if time.monotonic() < next_election:
                continue
            self._term += 1
            self._leader_id = ""
            self._lease_expires_at = 0
            self._persist(voted_for=self.node_id)
            votes = 1 + self._post_all(
                "/api/ha/internal/vote",
                {"term": self._term, "candidate_id": self.node_id},
            )
            self._peer_acks = max(0, votes - 1)
            if votes >= 2:
                self._lease_expires_at = now + self.lease_seconds
                self._lease_deadline_monotonic = time.monotonic() + self.lease_seconds
                self._set_role("leader", quorum=True, leader_id=self.node_id)
                self._persist(voted_for=self.node_id)
            else:
                self._set_role("follower", quorum=False, leader_id="")
            next_election = time.monotonic() + random.uniform(self.lease_seconds, self.lease_seconds * 1.5)

    def _watchdog_loop(self) -> None:
        """Fence outputs even when the election/HTTP thread stalls."""
        interval = min(0.25, max(0.05, self.heartbeat_seconds / 4.0))
        while not self._stop.wait(interval):
            with self._lock:
                expired = (
                    self._role == "leader"
                    and self._lease_deadline_monotonic > 0
                    and time.monotonic() >= self._lease_deadline_monotonic
                )
            if not expired:
                continue
            with self._lock:
                self._lease_expires_at = 0.0
            self._set_role("follower", quorum=False, leader_id="")
            try:
                self._persist(voted_for="")
            except Exception as exc:
                with self._lock:
                    self._last_error = f"lease_watchdog_persist: {exc}"[:500]

    def _anchor_audit_head(self) -> None:
        verification = audit_chain.verify()
        head = str(verification.get("head") or "")
        if not verification.get("valid") or not head:
            return
        for peer in self.peers:
            try:
                result = self._transport(
                    f"{peer}/api/ha/internal/audit-anchor",
                    {"entry_hash": head, "node_id": self.node_id},
                )
                anchored_at = str(result.get("anchored_at") or "")
                signature = str(result.get("signature") or "")
                secret = os.getenv("CLEANROOM_HA_SHARED_SECRET", "").encode("utf-8")
                expected = hmac.new(
                    secret,
                    f"{head}|{self.node_id}|{anchored_at}".encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest() if secret and anchored_at else ""
                if result.get("accepted") and result.get("witness") and expected and hmac.compare_digest(expected, signature):
                    audit_chain.anchor(head, f"{anchored_at}:{signature}")
                    self._last_anchor_at = time.time()
                    return
            except Exception:
                continue

    def receive_heartbeat(self, term: int, leader_id: str, lease_expires_at: float) -> bool:
        now = time.time()
        if int(term) < self._term or float(lease_expires_at) <= now:
            return False
        self._term = int(term)
        self._lease_expires_at = min(float(lease_expires_at), now + self.lease_seconds * 1.5)
        self._lease_deadline_monotonic = time.monotonic() + max(0.0, self._lease_expires_at - now)
        self._set_role("follower", quorum=True, leader_id=str(leader_id))
        self._persist(voted_for="")
        return True

    def grant_vote(self, term: int, candidate_id: str) -> bool:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM ha_state WHERE id=1").fetchone()
            current_term = int(row["current_term"] or 0)
            voted_for = str(row["voted_for"] or "")
            lease_expires = float(row["leader_lease_expires_at"] or 0)
            if int(term) < current_term or lease_expires > time.time():
                return False
            if int(term) == current_term and voted_for not in {"", str(candidate_id)}:
                return False
            conn.execute(
                "UPDATE ha_state SET current_term=?, voted_for=?, leader_id='', leader_lease_expires_at=0, updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (int(term), str(candidate_id)),
            )
            conn.commit()
            self._term = max(self._term, int(term))
            return True
        finally:
            conn.close()

    def snapshot(self) -> dict:
        with self._lock:
            payload = HaSnapshot(
                enabled=self.enabled,
                node_id=self.node_id,
                role=self._role,
                term=self._term,
                leader_id=self._leader_id,
                lease_expires_at=self._lease_expires_at,
                quorum=self._quorum,
                peer_acks=self._peer_acks,
                last_error=self._last_error,
            ).as_dict()
            if self.enabled and self._role == "leader":
                remaining = max(0.0, self._lease_deadline_monotonic - time.monotonic())
                payload["lease_remaining_seconds"] = round(remaining, 3)
                payload["safe_to_broadcast"] = bool(self._quorum and remaining > 0)
            return payload

    def require_safe_mutation(self, *, override_reason: str = "") -> dict:
        snapshot = self.snapshot()
        if not self.enabled or (snapshot["quorum"] and snapshot["role"] == "leader"):
            return snapshot
        if str(override_reason or "").strip():
            audit_chain.append(category="override", action="single_node_mutation", payload={"reason": override_reason, "ha": snapshot})
            return snapshot
        raise RuntimeError("ha_replication_not_ready")

    def replicate(self, entry: dict) -> int:
        if not self.enabled:
            return 0
        acknowledgements = self._post_all("/api/ha/internal/replicate", dict(entry or {}))
        if acknowledgements < 1:
            raise RuntimeError("ha_replication_ack_required")
        return acknowledgements

    def replicate_ordered(self, *, through_sequence: int | None = None, limit: int = 250) -> int:
        """Replicate pending journal records strictly in local commit order."""
        if not self.enabled:
            return 0
        from app.services.replication_journal import replication_journal

        replicated = 0
        for entry in replication_journal.pending(limit=limit):
            sequence = int(entry["sequence"])
            if through_sequence is not None and sequence > int(through_sequence):
                break
            self.replicate(entry)
            replication_journal.mark_replicated(sequence)
            replicated += 1
        return replicated

    def replicate_checkpoint(self, checkpoint: dict) -> int:
        if not self.enabled:
            return 0
        acknowledgements = self._post_all("/api/ha/internal/checkpoint", dict(checkpoint or {}))
        if acknowledgements < 1:
            raise RuntimeError("ha_checkpoint_ack_required")
        return acknowledgements


ha_coordinator = HaCoordinator()
