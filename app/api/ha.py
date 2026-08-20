from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_any_permission
from app.services.audit_chain import audit_chain
from app.services.ha_coordinator import ha_coordinator, validate_ha_token

router = APIRouter()


class HeartbeatPayload(BaseModel):
    term: int
    leader_id: str
    lease_expires_at: float


class VotePayload(BaseModel):
    term: int
    candidate_id: str


class AnchorPayload(BaseModel):
    entry_hash: str
    node_id: str = ""


class ReplicationPayload(BaseModel):
    sequence: int
    entity_type: str
    entity_id: str = ""
    operation: str
    payload_json: str
    checksum: str


class CheckpointPayload(BaseModel):
    node_id: str
    payload: dict
    payload_json: str
    checksum: str


def _internal_auth(token: str) -> None:
    if not validate_ha_token(token):
        raise HTTPException(status_code=401, detail="invalid_ha_token")


@router.get("/api/ha/status")
def ha_status(_user=Depends(require_any_permission("stations.view", "stream.failover"))):
    snapshot = ha_coordinator.snapshot()
    snapshot["audit_chain"] = audit_chain.verify()
    return snapshot


@router.post("/api/ha/internal/heartbeat")
def ha_heartbeat(payload: HeartbeatPayload, x_onair_ha_token: str = Header(default="")):
    _internal_auth(x_onair_ha_token)
    accepted = ha_coordinator.receive_heartbeat(payload.term, payload.leader_id, payload.lease_expires_at)
    return {"accepted": accepted, "node_id": ha_coordinator.node_id, "term": ha_coordinator.snapshot()["term"]}


@router.post("/api/ha/internal/vote")
def ha_vote(payload: VotePayload, x_onair_ha_token: str = Header(default="")):
    _internal_auth(x_onair_ha_token)
    accepted = ha_coordinator.grant_vote(payload.term, payload.candidate_id)
    return {"accepted": accepted, "node_id": ha_coordinator.node_id, "term": ha_coordinator.snapshot()["term"]}


@router.post("/api/ha/internal/audit-anchor")
def ha_audit_anchor(payload: AnchorPayload, x_onair_ha_token: str = Header(default="")):
    _internal_auth(x_onair_ha_token)
    created_at = str(int(time.time()))
    secret = os.getenv("CLEANROOM_HA_SHARED_SECRET", "").encode("utf-8")
    signature = hmac.new(secret, f"{payload.entry_hash}|{payload.node_id}|{created_at}".encode("utf-8"), hashlib.sha256).hexdigest()
    from app.db import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO witness_audit_anchors(entry_hash, node_id, signature, anchored_at) VALUES (?, ?, ?, ?)",
            (payload.entry_hash, payload.node_id, signature, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "accepted": True,
        "witness": bool(ha_coordinator.witness_only),
        "entry_hash": payload.entry_hash,
        "node_id": payload.node_id,
        "anchored_at": created_at,
        "signature": signature,
    }


@router.post("/api/ha/internal/replicate")
def ha_replicate(payload: ReplicationPayload, x_onair_ha_token: str = Header(default="")):
    _internal_auth(x_onair_ha_token)
    import hashlib
    from app.db import get_connection, init_db

    material = f"{payload.entity_type}|{payload.entity_id}|{payload.operation}|{payload.payload_json}"
    if not hmac.compare_digest(hashlib.sha256(material.encode("utf-8")).hexdigest(), payload.checksum):
        raise HTTPException(status_code=400, detail="replication_checksum_mismatch")
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO replication_journal(sequence, entity_type, entity_id, operation, payload_json, checksum, replicated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (payload.sequence, payload.entity_type, payload.entity_id, payload.operation, payload.payload_json, payload.checksum),
        )
        conn.commit()
    finally:
        conn.close()
    from app.services.replication_applier import replication_applier
    replication_applier.apply_pending()
    return {"accepted": True, "sequence": payload.sequence}


@router.post("/api/ha/internal/checkpoint")
def ha_checkpoint(payload: CheckpointPayload, x_onair_ha_token: str = Header(default="")):
    _internal_auth(x_onair_ha_token)
    from app.services.playout_checkpoint import playout_checkpoint_service
    playout_checkpoint_service.store(payload.model_dump())
    return {"accepted": True, "station_id": int(payload.payload.get("station_id") or 0)}
