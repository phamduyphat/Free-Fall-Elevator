"""Tamper-evident audit logging and integrity verification."""

import hashlib
import json
from datetime import datetime
from typing import Any

from .models import AuditIntegrityResult
from .storage import SQLiteStore


def audit_hash(
    sequence: int,
    timestamp: str,
    actor: str,
    action: str,
    outcome: str,
    details: str,
    previous_hash: str,
) -> str:
    payload = json.dumps(
        [
            sequence,
            timestamp,
            actor,
            action,
            outcome,
            details,
            previous_hash,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def record(
        self,
        actor: str,
        action: str,
        outcome: str,
        details: dict[str, Any],
        at: datetime,
    ) -> int:
        return self._store.append_audit(actor, action, outcome, details, at)

    def verify(self) -> AuditIntegrityResult:
        rows = self._store.audit_rows()
        anchored_sequence, anchored_hash = self._store.audit_anchor()
        previous_hash = ""
        expected_sequence = 1
        for row in rows:
            (
                sequence,
                timestamp,
                actor,
                action,
                outcome,
                details,
                stored_previous_hash,
                stored_hash,
            ) = row
            calculated = audit_hash(
                sequence,
                timestamp,
                actor,
                action,
                outcome,
                details,
                previous_hash,
            )
            if (
                sequence != expected_sequence
                or stored_previous_hash != previous_hash
                or stored_hash != calculated
            ):
                return AuditIntegrityResult(False, sequence, len(rows))
            previous_hash = stored_hash
            expected_sequence += 1
        if anchored_sequence != len(rows) or anchored_hash != previous_hash:
            first_missing = len(rows) + 1 if anchored_sequence > len(rows) else anchored_sequence
            return AuditIntegrityResult(False, first_missing, len(rows))
        return AuditIntegrityResult(True, None, len(rows))
