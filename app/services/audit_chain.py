from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.db import get_connection, init_db


def _canonical(payload: dict | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuditChain:
    """Append-only, hash-linked security and operator audit records."""

    def append(
        self,
        *,
        category: str,
        action: str,
        payload: dict | None = None,
        station_id: int | None = None,
        actor_id: int | None = None,
        conn=None,
    ) -> dict:
        init_db()
        owned = conn is None
        connection = conn or get_connection()
        try:
            cur = connection.cursor()
            if not connection.in_transaction:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT entry_hash FROM audit_chain ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            previous = str(row["entry_hash"] if row else "")
            created_at = datetime.now(timezone.utc).isoformat()
            payload_json = _canonical(payload)
            material = "|".join(
                [
                    previous,
                    str(int(station_id) if station_id is not None else ""),
                    str(category or "operation"),
                    str(action or "unknown"),
                    str(int(actor_id) if actor_id is not None else ""),
                    payload_json,
                    created_at,
                ]
            )
            entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO audit_chain(station_id, category, action, actor_id, payload_json, previous_hash, entry_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (station_id, category, action, actor_id, payload_json, previous, entry_hash, created_at),
            )
            connection.commit()
            return {"id": int(cur.lastrowid), "entry_hash": entry_hash, "previous_hash": previous, "created_at": created_at}
        finally:
            if owned:
                connection.close()

    def anchor(self, entry_hash: str, anchor: str) -> None:
        init_db()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE audit_chain SET witness_anchor=? WHERE entry_hash=?",
                (str(anchor or ""), str(entry_hash or "")),
            )
            conn.commit()
        finally:
            conn.close()

    def verify(self) -> dict:
        init_db()
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM audit_chain ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        previous = ""
        for row in rows:
            payload_json = _canonical(json.loads(str(row["payload_json"] or "{}")))
            material = "|".join(
                [
                    previous,
                    str(row["station_id"] if row["station_id"] is not None else ""),
                    str(row["category"]),
                    str(row["action"]),
                    str(row["actor_id"] if row["actor_id"] is not None else ""),
                    payload_json,
                    str(row["created_at"]),
                ]
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if str(row["previous_hash"]) != previous or str(row["entry_hash"]) != expected:
                return {"valid": False, "count": len(rows), "failed_id": int(row["id"])}
            previous = expected
        return {"valid": True, "count": len(rows), "head": previous}


audit_chain = AuditChain()
