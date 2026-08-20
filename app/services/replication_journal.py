from __future__ import annotations

import hashlib
import json

from app.db import get_connection, init_db


def canonical_json(payload: dict | list | None) -> str:
    return json.dumps(payload if payload is not None else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ReplicationJournal:
    def append(self, entity_type: str, entity_id: str | int, operation: str, payload) -> dict:
        init_db()
        body = canonical_json(payload)
        material = f"{entity_type}|{entity_id}|{operation}|{body}"
        checksum = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO replication_journal(entity_type, entity_id, operation, payload_json, checksum) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(entity_type), str(entity_id), str(operation), body, checksum),
            )
            conn.commit()
            row = conn.execute("SELECT sequence, replicated_at FROM replication_journal WHERE checksum=?", (checksum,)).fetchone()
            return {"sequence": int(row["sequence"]), "checksum": checksum, "replicated": bool(row["replicated_at"])}
        finally:
            conn.close()

    def pending(self, limit: int = 250) -> list[dict]:
        init_db()
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM replication_journal WHERE replicated_at IS NULL ORDER BY sequence ASC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_replicated(self, sequence: int) -> None:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE replication_journal SET replicated_at=CURRENT_TIMESTAMP WHERE sequence=?",
                (int(sequence),),
            )
            conn.commit()
        finally:
            conn.close()


replication_journal = ReplicationJournal()
