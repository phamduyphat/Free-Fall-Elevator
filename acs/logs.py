from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Optional
import uuid

from db import DbConnectionPool


class AuthorizationOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class DenialReason(StrEnum):
    UNREGISTERED_CARD = "UNREGISTERED_CARD"
    CARD_SUSPENDED = "CARD_SUSPENDED"
    CARD_REVOKED = "CARD_REVOKED"
    CARD_EXPIRED = "CARD_EXPIRED"
    UNAUTHORIZED_FLOOR = "UNAUTHORIZED_FLOOR"
    E5_B1_FORBIDDEN = "E5_B1_FORBIDDEN"
    ONE_TAP_LIMIT_EXCEEDED = "ONE_TAP_LIMIT_EXCEEDED"
    TOKEN_WINDOW_EXPIRED = "TOKEN_WINDOW_EXPIRED"
    NO_ACTIVE_CREDENTIAL = "NO_ACTIVE_CREDENTIAL"
    DB_FAIL_CLOSED = "DB_FAIL_CLOSED"
    MALFORMED_MSG_REJECTED = "MALFORMED_MSG_REJECTED"
    UNAUTHORIZED_CMD_REJECTED = "UNAUTHORIZED_CMD_REJECTED"
    COMMAND_CONFLICT_REJECTED = "COMMAND_CONFLICT_REJECTED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    PEER_ADMIN_REQUIRED = "PEER_ADMIN_REQUIRED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    CREDENTIAL_ALREADY_EXISTS = "CREDENTIAL_ALREADY_EXISTS"
    REPLACEMENT_ID_IN_USE = "REPLACEMENT_ID_IN_USE"
    DASHBOARD_ACCESS_DENIED = "DASHBOARD_ACCESS_DENIED"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    UNAUTHORIZED_ROLE = "UNAUTHORIZED_ROLE"


class AlertSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertType(StrEnum):
    REPEATED_FAILURES = "REPEATED_FAILURES"
    CLONED_CREDENTIAL = "CLONED_CREDENTIAL"
    CARD_SCANNING = "CARD_SCANNING"
    LOST_CARD_USAGE = "LOST_CARD_USAGE"
    REVOKED_CARD_USAGE = "REVOKED_CARD_USAGE"
    DOOR_OBSTRUCTION = "DOOR_OBSTRUCTION"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AuditActionType(StrEnum):
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOGOUT = "ADMIN_LOGOUT"
    ACCESS_DENIAL = "ACCESS_DENIAL"
    ONE_TAP_VIOLATION = "ONE_TAP_VIOLATION"
    CREDENTIAL_ISSUED = "CREDENTIAL_ISSUED"
    CREDENTIAL_SUSPENDED = "CREDENTIAL_SUSPENDED"
    CREDENTIAL_REINSTATED = "CREDENTIAL_REINSTATED"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    CREDENTIAL_REPLACED = "CREDENTIAL_REPLACED"
    VISITOR_PASS_ISSUED = "VISITOR_PASS_ISSUED"
    VISITOR_PASS_EXPIRED = "VISITOR_PASS_EXPIRED"
    POLICY_CHANGED = "POLICY_CHANGED"
    CALL_CANCELLED_BY_ADMIN = "CALL_CANCELLED_BY_ADMIN"
    E5_SERVICE_RUN_START = "E5_SERVICE_RUN_START"
    E5_SERVICE_RUN_END = "E5_SERVICE_RUN_END"
    EMERGENCY_OVERRIDE = "EMERGENCY_OVERRIDE"
    RESCUE_CAR_DISPATCHED = "RESCUE_CAR_DISPATCHED"
    RESCUE_CAR_BOARDED = "RESCUE_CAR_BOARDED"
    RESCUE_CAR_RELEASED = "RESCUE_CAR_RELEASED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    FALSE_ALARM_CLOSED = "FALSE_ALARM_CLOSED"
    MAINTENANCE_ENTER = "MAINTENANCE_ENTER"
    RETURN_TO_SERVICE = "RETURN_TO_SERVICE"
    MALFORMED_MSG_REJECTED = "MALFORMED_MSG_REJECTED"
    COMMAND_CONFLICT_REJECTED = "COMMAND_CONFLICT_REJECTED"
    UNAUTHORIZED_CMD_REJECTED = "UNAUTHORIZED_CMD_REJECTED"
    INTEGRITY_CHECK_RUN = "INTEGRITY_CHECK_RUN"
    REPORT_EXPORTED = "REPORT_EXPORTED"
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    DATA_BACKUP_CREATED = "DATA_BACKUP_CREATED"
    DATA_RESTORE_COMPLETED = "DATA_RESTORE_COMPLETED"


@dataclass
class IntegrityCheckResult:
    passed: bool
    total_records_inspected: int
    mismatch_index: Optional[int] = None
    reason: Optional[str] = None


class Logging:
    """
    ACS.Logging submodule (`acs/logs.py`).
    Centralizes persistence for authorization decisions, security alerts,
    and the cryptographic SHA-256 tamper-evident audit hash chain (`audit_log_chain`).
    """

    GENESIS_PREV_HASH: str = "0" * 64

    @staticmethod
    def normalize_timestamp(ts: datetime | str) -> datetime:
        """Normalize a timestamp to a naive datetime suitable for PostgreSQL TIMESTAMP columns."""
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is not None:
            return ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts

    @staticmethod
    def format_timestamp(ts: datetime | str) -> str:
        """Deterministically format a timestamp to microsecond precision for SHA-256 hashing."""
        return Logging.normalize_timestamp(ts).isoformat(sep=" ", timespec="microseconds")

    @staticmethod
    def canonicalize_payload(event_payload: dict[str, Any] | str) -> str:
        """Deterministically serialize an event payload for SHA-256 hashing."""
        if isinstance(event_payload, str):
            try:
                parsed = json.loads(event_payload)
                return json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str)
            except json.JSONDecodeError:
                return json.dumps(
                    {"raw": event_payload}, sort_keys=True, separators=(",", ":"), default=str
                )
        return json.dumps(event_payload, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def compute_audit_hash(
        log_index: int,
        timestamp: datetime | str,
        actor_id: str,
        action_type: str,
        canonical_payload: str,
        prev_hash: str,
    ) -> str:
        """
        Compute SHA-256(log_index || timestamp || actor_id || action_type || event_payload || prev_hash)
        as specified in Section 5.5 of design_decisions_ACS.md.
        """
        formatted_ts = Logging.format_timestamp(timestamp)
        preimage = (
            f"{log_index}||{formatted_ts}||{actor_id}||{action_type}||"
            f"{canonical_payload}||{prev_hash}"
        )
        return hashlib.sha256(preimage.encode("utf-8")).hexdigest()

    @staticmethod
    async def write_audit(
        *,
        timestamp: datetime,
        actor_id: str,
        actor_role: str,
        action_type: AuditActionType | str,
        target_id: str,
        event_payload: dict[str, Any] | str,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> dict[str, Any]:
        """Append a tamper-evident record to `audit_log_chain`."""
        normalized_ts = Logging.normalize_timestamp(timestamp)
        canonical_payload = Logging.canonicalize_payload(event_payload)
        action_type_str = str(action_type)
        actor_role_str = str(actor_role)

        async with db_pool.acquire() as conn:
            last_row = await conn.fetchrow(
                """
                SELECT log_index, current_hash
                FROM audit_log_chain
                ORDER BY log_index DESC
                LIMIT 1
                FOR UPDATE
                """
            )
            if last_row is None:
                next_index = 1
                prev_hash = Logging.GENESIS_PREV_HASH
            else:
                next_index = int(last_row["log_index"]) + 1
                prev_hash = str(last_row["current_hash"])

            current_hash = Logging.compute_audit_hash(
                log_index=next_index,
                timestamp=normalized_ts,
                actor_id=actor_id,
                action_type=action_type_str,
                canonical_payload=canonical_payload,
                prev_hash=prev_hash,
            )

            await conn.execute(
                """
                INSERT INTO audit_log_chain (
                    log_index,
                    timestamp,
                    actor_id,
                    actor_role,
                    action_type,
                    target_id,
                    event_payload,
                    prev_hash,
                    current_hash
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                """,
                next_index,
                normalized_ts,
                actor_id,
                actor_role_str,
                action_type_str,
                target_id,
                canonical_payload,
                prev_hash,
                current_hash,
            )

        return {
            "log_index": next_index,
            "timestamp": normalized_ts,
            "actor_id": actor_id,
            "actor_role": actor_role_str,
            "action_type": action_type_str,
            "target_id": target_id,
            "event_payload": json.loads(canonical_payload),
            "prev_hash": prev_hash,
            "current_hash": current_hash,
        }

    @staticmethod
    async def log_authorization_event(
        *,
        timestamp: datetime,
        credential_id: Optional[str],
        elevator_id: str,
        origin_floor: int,
        destination_floor: int,
        outcome: AuthorizationOutcome | str,
        denial_reason: Optional[DenialReason | str] = None,
        alert_triggered: bool = False,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> str:
        """Record an operational decision or denial in `access_authorization_events`."""
        event_id = str(uuid.uuid4())
        normalized_ts = Logging.normalize_timestamp(timestamp)
        query = """
            INSERT INTO access_authorization_events (
                event_id,
                timestamp,
                credential_id,
                elevator_id,
                origin_floor,
                destination_floor,
                outcome,
                denial_reason,
                alert_triggered
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """
        async with db_pool.acquire() as conn:
            await conn.execute(
                query,
                event_id,
                normalized_ts,
                credential_id,
                str(elevator_id),
                origin_floor,
                destination_floor,
                str(outcome),
                str(denial_reason) if denial_reason is not None else None,
                alert_triggered,
            )
        return event_id

    @staticmethod
    async def create_security_alert(
        *,
        severity: AlertSeverity | str,
        alert_type: AlertType | str,
        details: str,
        created_at: datetime,
        credential_id: Optional[str] = None,
        primary_car_id: Optional[str] = None,
        secondary_car_id: Optional[str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> str:
        """Create a security incident record in `security_alerts` (`SEC-2.8`)."""
        alert_id = str(uuid.uuid4())
        normalized_ts = Logging.normalize_timestamp(created_at)
        query = """
            INSERT INTO security_alerts (
                alert_id,
                severity,
                alert_type,
                credential_id,
                primary_car_id,
                secondary_car_id,
                details,
                status,
                resolved_by,
                created_at,
                resolved_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, $9, NULL)
        """
        async with db_pool.acquire() as conn:
            await conn.execute(
                query,
                alert_id,
                str(severity),
                str(alert_type),
                credential_id,
                str(primary_car_id) if primary_car_id is not None else None,
                str(secondary_car_id) if secondary_car_id is not None else None,
                details,
                AlertStatus.OPEN.value,
                normalized_ts,
            )
        return alert_id

    @staticmethod
    async def update_security_alert_status(
        *,
        alert_id: str,
        new_status: AlertStatus | str,
        admin_user_id: str,
        timestamp: datetime,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> bool:
        """Acknowledge or resolve a security alert (`OPEN` -> `ACKNOWLEDGED` | `RESOLVED`)."""
        try:
            validated_status = AlertStatus(new_status)
        except ValueError as exc:
            raise ValueError(f"Invalid alert status: {new_status}") from exc

        normalized_ts = Logging.normalize_timestamp(timestamp)
        resolved_by = admin_user_id if validated_status == AlertStatus.RESOLVED else None
        resolved_at = normalized_ts if validated_status == AlertStatus.RESOLVED else None

        async with db_pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE security_alerts
                SET status = $2,
                    resolved_by = COALESCE($3, resolved_by),
                    resolved_at = COALESCE($4, resolved_at)
                WHERE alert_id = $1
                """,
                alert_id,
                validated_status.value,
                resolved_by,
                resolved_at,
            )
            return result.endswith("1")

    @staticmethod
    async def verify_audit_chain(
        *,
        admin_credential_id: Optional[str] = None,
        admin_role: str = "ADMIN",
        timestamp: Optional[datetime] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> IntegrityCheckResult:
        """
        Verify the cryptographic integrity of `audit_log_chain` (`ANA-5.8, VT-10`).
        Optionally writes an `INTEGRITY_CHECK_RUN` audit entry when `admin_credential_id` is supplied.
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    log_index,
                    timestamp,
                    actor_id,
                    actor_role,
                    action_type,
                    target_id,
                    event_payload::text AS event_payload_text,
                    prev_hash,
                    current_hash
                FROM audit_log_chain
                ORDER BY log_index ASC
                """
            )

        expected_prev_hash = Logging.GENESIS_PREV_HASH
        expected_index = 1
        inspected = 0
        check_result: Optional[IntegrityCheckResult] = None

        for row in rows:
            inspected += 1
            actual_index = int(row["log_index"])
            if actual_index != expected_index:
                check_result = IntegrityCheckResult(
                    passed=False,
                    total_records_inspected=inspected,
                    mismatch_index=actual_index,
                    reason=f"Non-monotonic log_index: expected {expected_index}, found {actual_index}",
                )
                break

            actual_prev_hash = str(row["prev_hash"])
            if actual_prev_hash != expected_prev_hash:
                check_result = IntegrityCheckResult(
                    passed=False,
                    total_records_inspected=inspected,
                    mismatch_index=actual_index,
                    reason="prev_hash mismatch against preceding record's current_hash",
                )
                break

            canonical_payload = Logging.canonicalize_payload(row["event_payload_text"])
            recomputed_hash = Logging.compute_audit_hash(
                log_index=actual_index,
                timestamp=row["timestamp"],
                actor_id=str(row["actor_id"]),
                action_type=str(row["action_type"]),
                canonical_payload=canonical_payload,
                prev_hash=actual_prev_hash,
            )

            if recomputed_hash != str(row["current_hash"]):
                check_result = IntegrityCheckResult(
                    passed=False,
                    total_records_inspected=inspected,
                    mismatch_index=actual_index,
                    reason="current_hash mismatch (record payload or metadata tampered)",
                )
                break

            expected_prev_hash = str(row["current_hash"])
            expected_index += 1

        if check_result is None:
            check_result = IntegrityCheckResult(
                passed=True,
                total_records_inspected=inspected,
                mismatch_index=None,
                reason=None,
            )

        if admin_credential_id is not None and check_result.passed:
            await Logging.write_audit(
                timestamp=timestamp or datetime.now(),
                actor_id=admin_credential_id,
                actor_role=admin_role,
                action_type=AuditActionType.INTEGRITY_CHECK_RUN,
                target_id="audit_log_chain",
                event_payload={
                    "total_records_inspected": check_result.total_records_inspected,
                    "result": "PASS",
                    "mismatch_index": None,
                },
                db_pool=db_pool,
            )

        return check_result

    @staticmethod
    async def search_authorization_events(
        *,
        credential_id: Optional[str] = None,
        elevator_id: Optional[str] = None,
        outcome: Optional[AuthorizationOutcome | str] = None,
        denial_reason: Optional[DenialReason | str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> list[dict[str, Any]]:
        """Query `access_authorization_events` with optional filters (`SEC-2.14`)."""
        clauses: list[str] = []
        params: list[Any] = []

        if credential_id is not None:
            params.append(credential_id)
            clauses.append(f"credential_id = ${len(params)}")
        if elevator_id is not None:
            params.append(str(elevator_id))
            clauses.append(f"elevator_id = ${len(params)}")
        if outcome is not None:
            params.append(str(outcome))
            clauses.append(f"outcome = ${len(params)}")
        if denial_reason is not None:
            params.append(str(denial_reason))
            clauses.append(f"denial_reason = ${len(params)}")
        if start_time is not None:
            params.append(Logging.normalize_timestamp(start_time))
            clauses.append(f"timestamp >= ${len(params)}")
        if end_time is not None:
            params.append(Logging.normalize_timestamp(end_time))
            clauses.append(f"timestamp <= ${len(params)}")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"""
            SELECT *
            FROM access_authorization_events
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT ${len(params)}
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    @staticmethod
    async def search_security_alerts(
        *,
        status: Optional[AlertStatus | str] = None,
        severity: Optional[AlertSeverity | str] = None,
        alert_type: Optional[AlertType | str] = None,
        credential_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> list[dict[str, Any]]:
        """Query `security_alerts` with optional filters (`SEC-2.14`)."""
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            params.append(str(status))
            clauses.append(f"status = ${len(params)}")
        if severity is not None:
            params.append(str(severity))
            clauses.append(f"severity = ${len(params)}")
        if alert_type is not None:
            params.append(str(alert_type))
            clauses.append(f"alert_type = ${len(params)}")
        if credential_id is not None:
            params.append(credential_id)
            clauses.append(f"credential_id = ${len(params)}")
        if start_time is not None:
            params.append(Logging.normalize_timestamp(start_time))
            clauses.append(f"created_at >= ${len(params)}")
        if end_time is not None:
            params.append(Logging.normalize_timestamp(end_time))
            clauses.append(f"created_at <= ${len(params)}")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"""
            SELECT *
            FROM security_alerts
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${len(params)}
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    @staticmethod
    async def search_audit_logs(
        *,
        actor_id: Optional[str] = None,
        action_type: Optional[AuditActionType | str] = None,
        target_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> list[dict[str, Any]]:
        """Query `audit_log_chain` with optional filters (`SEC-2.14`)."""
        clauses: list[str] = []
        params: list[Any] = []

        if actor_id is not None:
            params.append(actor_id)
            clauses.append(f"actor_id = ${len(params)}")
        if action_type is not None:
            params.append(str(action_type))
            clauses.append(f"action_type = ${len(params)}")
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"target_id = ${len(params)}")
        if start_time is not None:
            params.append(Logging.normalize_timestamp(start_time))
            clauses.append(f"timestamp >= ${len(params)}")
        if end_time is not None:
            params.append(Logging.normalize_timestamp(end_time))
            clauses.append(f"timestamp <= ${len(params)}")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"""
            SELECT *
            FROM audit_log_chain
            {where_sql}
            ORDER BY log_index DESC
            LIMIT ${len(params)}
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                if isinstance(item.get("event_payload"), str):
                    try:
                        item["event_payload"] = json.loads(item["event_payload"])
                    except json.JSONDecodeError:
                        pass
                result.append(item)
            return result

    @staticmethod
    async def search_incidents(
        *,
        status: Optional[str] = None,
        emergency_type: Optional[str] = None,
        elevator_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> list[dict[str, Any]]:
        """Query `incidents` with optional filters (`ANA-5.7, SEC-2.14, EMR-3.4`)."""
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            params.append(str(status))
            clauses.append(f"status = ${len(params)}")
        if emergency_type is not None:
            params.append(str(emergency_type))
            clauses.append(f"emergency_type = ${len(params)}")
        if elevator_id is not None:
            params.append(str(elevator_id))
            clauses.append(f"elevator_id = ${len(params)}")
        if start_time is not None:
            params.append(Logging.normalize_timestamp(start_time))
            clauses.append(f"created_at >= ${len(params)}")
        if end_time is not None:
            params.append(Logging.normalize_timestamp(end_time))
            clauses.append(f"created_at <= ${len(params)}")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"""
            SELECT *
            FROM incidents
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${len(params)}
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    search_audit_records = search_audit_logs
