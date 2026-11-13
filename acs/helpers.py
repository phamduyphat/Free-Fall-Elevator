from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Optional

import asyncpg

from acs.enums import (
    AlertSeverity,
    AlertType,
    AuditActionType,
    CredentialStatus,
    DenialReason,
    ElevatorId,
    SuspensionReason,
    UserRole,
)
from acs.logs import Logging
from acs.payloads import AuthorizationResult, CardSwipePayload
from db import DbConnectionPool

ALL_BUILDING_FLOORS: list[int] = [-1] + list(range(1, 31))
COMMON_FLOORS: list[int] = [-1, 1]

REPEATED_FAILURES_WINDOW_SECONDS: int = 60
REPEATED_FAILURES_THRESHOLD: int = 3
SCANNING_WINDOW_SECONDS: int = 300  # 5 minutes
SCANNING_UNIQUE_CARDS_THRESHOLD: int = 11
LOST_CARD_REVIEW_WINDOW_SECONDS: int = 72 * 3600  # 72 hours

# Shared anomaly detection windows across cabin and dashboard readers
failed_attempts_counter: dict[str, list[datetime]] = {}
unregistered_scan_counter: list[tuple[str, datetime]] = []


def reset_shared_state() -> None:
    """Clear shared in-memory anomaly detection windows."""
    failed_attempts_counter.clear()
    unregistered_scan_counter.clear()


def parse_allowed_floors(
    raw_floors: Any,
    role: str,
    assigned_floor: Optional[int] = None,
) -> list[int]:
    if role == UserRole.ADMIN:
        return list(ALL_BUILDING_FLOORS)

    floors: list[int] = []
    if isinstance(raw_floors, str):
        try:
            parsed = json.loads(raw_floors)
            if isinstance(parsed, list):
                floors = [int(f) for f in parsed]
        except (json.JSONDecodeError, ValueError, TypeError):
            floors = []
    elif isinstance(raw_floors, (list, tuple, set)):
        floors = [int(f) for f in raw_floors]

    floor_set = set(floors) | set(COMMON_FLOORS)
    if role == UserRole.RESIDENT and assigned_floor is not None:
        floor_set.add(int(assigned_floor))

    return sorted(floor_set)


def fail_closed(credential_id: Optional[str] = None) -> AuthorizationResult:
    return AuthorizationResult.deny(
        DenialReason.DB_FAIL_CLOSED,
        message="Database error: fail-closed policy enforced.",
        credential_id=credential_id,
    )


async def reject_unauthorized_role(
    *,
    timestamp: datetime,
    actor_id: str,
    actor_role: str,
    target_id: str,
    operation: str,
    required_roles: list[str],
    message: str,
    extra_payload: Optional[dict[str, Any]] = None,
    db_pool: type[DbConnectionPool] = DbConnectionPool,
) -> AuthorizationResult:
    payload: dict[str, Any] = {
        "attempted_operation": operation,
        "missing_required_role": required_roles,
    }
    if extra_payload:
        payload.update(extra_payload)
    await Logging.write_audit(
        timestamp=timestamp,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
        target_id=target_id,
        event_payload=payload,
        db_pool=db_pool,
    )
    return AuthorizationResult.deny(
        DenialReason.UNAUTHORIZED_CMD_REJECTED,
        message=message,
        credential_id=actor_id,
    )


async def authorize_credential_role(
    conn: asyncpg.Connection,
    credential_id: str,
    ts: datetime,
    *,
    required_roles: list[str],
    inactive_msg: str,
    unauth_msg: str,
    target_id: str,
    operation: str,
    extra_payload: Optional[dict[str, Any]] = None,
    db_pool: type[DbConnectionPool] = DbConnectionPool,
) -> tuple[Optional[dict[str, Any]], Optional[AuthorizationResult]]:
    record = await fetch_and_refresh_credential(
        conn, credential_id, ts, db_pool=db_pool
    )
    if record is None or record["status"] != CredentialStatus.ACTIVE:
        reason = (
            DenialReason.UNREGISTERED_CARD
            if record is None
            else DenialReason(f"CARD_{record['status']}")
        )
        return None, AuthorizationResult.deny(
            reason, message=inactive_msg, credential_id=credential_id
        )

    role = record["user_role"]
    if role not in required_roles:
        return None, await reject_unauthorized_role(
            timestamp=ts,
            actor_id=credential_id,
            actor_role=role,
            target_id=target_id,
            operation=operation,
            required_roles=required_roles,
            message=unauth_msg.format(role=role),
            extra_payload=extra_payload,
            db_pool=db_pool,
        )
    return record, None


async def reject_malformed_msg(
    *,
    timestamp: datetime,
    target_id: str,
    source: str,
    payload_details: dict[str, Any],
    message: str,
    credential_id: Optional[str] = None,
    db_pool: type[DbConnectionPool] = DbConnectionPool,
) -> AuthorizationResult:
    try:
        await Logging.write_audit(
            timestamp=timestamp,
            actor_id=UserRole.SYSTEM,
            actor_role=UserRole.SYSTEM,
            action_type=AuditActionType.MALFORMED_MSG_REJECTED,
            target_id=target_id,
            event_payload={"source": source, **payload_details},
            db_pool=db_pool,
        )
    except Exception:
        return fail_closed(credential_id)
    return AuthorizationResult.deny(
        DenialReason.MALFORMED_MSG_REJECTED,
        message=message,
        credential_id=credential_id,
    )


async def record_failed_attempt(
    *,
    credential_id: str,
    car_id: Optional[str],
    timestamp: datetime,
    reason: str,
    db_pool: type[DbConnectionPool],
) -> bool:
    cutoff = timestamp - timedelta(seconds=REPEATED_FAILURES_WINDOW_SECONDS)
    history = [
        t for t in failed_attempts_counter.get(credential_id, []) if t >= cutoff
    ]
    history.append(timestamp)
    failed_attempts_counter[credential_id] = history

    if len(history) >= REPEATED_FAILURES_THRESHOLD:
        await Logging.create_security_alert(
            severity=AlertSeverity.MEDIUM,
            alert_type=AlertType.REPEATED_FAILURES,
            credential_id=credential_id,
            primary_car_id=car_id if ElevatorId.has_value(car_id) else None,
            details=(
                f"Credential '{credential_id}' accumulated {len(history)} failed attempts "
                f"within {REPEATED_FAILURES_WINDOW_SECONDS}s (latest reason: {reason})."
            ),
            created_at=timestamp,
            db_pool=db_pool,
        )
        return True
    return False


async def record_unregistered_scan(
    *,
    credential_id: str,
    car_id: Optional[str],
    timestamp: datetime,
    db_pool: type[DbConnectionPool],
) -> bool:
    global unregistered_scan_counter
    cutoff = timestamp - timedelta(seconds=SCANNING_WINDOW_SECONDS)
    unregistered_scan_counter = [
        (cid, t) for cid, t in unregistered_scan_counter if t >= cutoff
    ]
    unregistered_scan_counter.append((credential_id, timestamp))

    unique_ids = {cid for cid, _ in unregistered_scan_counter}
    if len(unique_ids) >= SCANNING_UNIQUE_CARDS_THRESHOLD:
        await Logging.create_security_alert(
            severity=AlertSeverity.HIGH,
            alert_type=AlertType.CARD_SCANNING,
            credential_id=credential_id,
            primary_car_id=car_id if ElevatorId.has_value(car_id) else None,
            details=(
                f"Card scanning brute-force detected: {len(unique_ids)} distinct unregistered "
                f"credential IDs presented within {SCANNING_WINDOW_SECONDS}s."
            ),
            created_at=timestamp,
            db_pool=db_pool,
        )
        return True
    return False


async def apply_credential_transitions(
    rec: dict[str, Any],
    current_time: datetime,
    conn: asyncpg.Connection,
    db_pool: type[DbConnectionPool] = DbConnectionPool,
) -> dict[str, Any]:
    """
    On-demand JIT state transition engine for a single credential record:
    - 4h visitor pass expiration (SEC-2.22) -> transitions ACTIVE to EXPIRED
    - 72h lost-card review window (SEC-2.17) -> transitions SUSPENDED to REVOKED
    """
    cid = rec["credential_id"]
    status = rec["status"]
    review_window = timedelta(seconds=LOST_CARD_REVIEW_WINDOW_SECONDS)

    # 1. Visitor Pass / Time-Limited Credential Expiration (SEC-2.22, VT-31)
    if status == CredentialStatus.ACTIVE and rec.get("expires_at") is not None:
        expires_at_dt = Logging.normalize_timestamp(rec["expires_at"])
        created_at_dt = Logging.normalize_timestamp(rec["created_at"])
        if current_time >= expires_at_dt:
            await conn.execute(
                "UPDATE credentials SET status = $2, updated_at = $3 WHERE credential_id = $1",
                cid,
                CredentialStatus.EXPIRED.value,
                current_time,
            )
            rec["status"] = CredentialStatus.EXPIRED.value
            rec["updated_at"] = current_time
            await Logging.write_audit(
                timestamp=current_time,
                actor_id=UserRole.SYSTEM,
                actor_role=UserRole.SYSTEM,
                action_type=AuditActionType.VISITOR_PASS_EXPIRED,
                target_id=cid,
                event_payload={
                    "credential_id": cid,
                    "expires_at": Logging.format_timestamp(expires_at_dt),
                    "total_valid_duration_seconds": (
                        expires_at_dt - created_at_dt
                    ).total_seconds(),
                    "final_status": CredentialStatus.EXPIRED.value,
                },
                db_pool=db_pool,
            )

    # 2. 72-Hour Lost Card Review Window Auto-Revocation (SEC-2.17)
    elif (
        status == CredentialStatus.SUSPENDED
        and rec.get("suspension_reason") == SuspensionReason.LOST
        and rec.get("suspension_start") is not None
    ):
        suspension_start_dt = Logging.normalize_timestamp(rec["suspension_start"])
        if (current_time - suspension_start_dt) >= review_window:
            await conn.execute(
                """
                UPDATE credentials
                SET status = $2,
                    revocation_reason = 'WINDOW_EXPIRED',
                    revoked_at = $3,
                    updated_at = $3
                WHERE credential_id = $1
                """,
                cid,
                CredentialStatus.REVOKED.value,
                current_time,
            )
            rec["status"] = CredentialStatus.REVOKED.value
            rec["revocation_reason"] = "WINDOW_EXPIRED"
            rec["revoked_at"] = current_time
            rec["updated_at"] = current_time
            await Logging.write_audit(
                timestamp=current_time,
                actor_id=UserRole.SYSTEM,
                actor_role=UserRole.SYSTEM,
                action_type=AuditActionType.CREDENTIAL_REVOKED,
                target_id=cid,
                event_payload={
                    "credential_id": cid,
                    "trigger": "WINDOW_EXPIRED",
                    "suspension_start": Logging.format_timestamp(suspension_start_dt),
                    "revoked_at": Logging.format_timestamp(current_time),
                },
                db_pool=db_pool,
            )

    return rec


async def fetch_and_refresh_credential(
    conn: asyncpg.Connection,
    credential_id: str,
    current_time: datetime,
    db_pool: type[DbConnectionPool] = DbConnectionPool,
) -> Optional[dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT
            c.credential_id, c.user_id, c.credential_type, c.status,
            c.suspension_reason, c.suspension_start, c.revocation_reason,
            c.revoked_at, c.expires_at, c.allowed_floors, c.issued_by_admin,
            c.created_at, c.updated_at,
            u.full_name, u.role AS user_role, u.apartment_unit, u.assigned_floor
        FROM credentials c
        JOIN users u ON c.user_id = u.user_id
        WHERE c.credential_id = $1
        """,
        credential_id,
    )
    if row is None:
        return None
    return await apply_credential_transitions(dict(row), current_time, conn, db_pool)


async def verify_swipe_credential(
    conn: asyncpg.Connection,
    payload: CardSwipePayload,
    ts: datetime,
    db_pool: type[DbConnectionPool],
) -> tuple[Optional[dict[str, Any]], Optional[AuthorizationResult]]:
    """Fetch, refresh, and verify active credential status for card swipes."""
    record = await fetch_and_refresh_credential(
        conn, payload.credential_id, ts, db_pool
    )
    if record is not None and record["status"] == CredentialStatus.ACTIVE:
        return record, None

    target_label = "DASHBOARD" if payload.is_dashboard_access else str(payload.elevator_id)
    car_id_or_none = None if payload.is_dashboard_access else str(payload.elevator_id)
    alert_type: Optional[AlertType] = None
    alert_severity: Optional[AlertSeverity] = None
    alert_details: Optional[str] = None
    scan_alert = False

    if record is None:
        denial_reason = DenialReason.UNREGISTERED_CARD
        actor_role = UserRole.SYSTEM
        message = "Unregistered credential presented."
        scan_alert = await record_unregistered_scan(
            credential_id=payload.credential_id,
            car_id=target_label,
            timestamp=ts,
            db_pool=db_pool,
        )
    else:
        status = record["status"]
        actor_role = record["user_role"]
        message = f"Credential is {status}."
        if status == CredentialStatus.SUSPENDED:
            denial_reason = DenialReason.CARD_SUSPENDED
            alert_severity = AlertSeverity.HIGH
            alert_type = (
                AlertType.CLONED_CREDENTIAL
                if record.get("suspension_reason") == SuspensionReason.CLONE_SUSPECTED
                else AlertType.LOST_CARD_USAGE
            )
            alert_details = (
                f"Suspended credential '{payload.credential_id}' "
                f"(reason: {record.get('suspension_reason')}) tapped at {target_label}."
            )
        elif status == CredentialStatus.REVOKED:
            denial_reason = DenialReason.CARD_REVOKED
            alert_severity = AlertSeverity.HIGH
            alert_type = AlertType.REVOKED_CARD_USAGE
            alert_details = f"Revoked credential '{payload.credential_id}' tapped at {target_label}."
        else:
            denial_reason = DenialReason.CARD_EXPIRED

    repeat_alert = await record_failed_attempt(
        credential_id=payload.credential_id,
        car_id=target_label,
        timestamp=ts,
        reason=denial_reason,
        db_pool=db_pool,
    )
    alert_fired = repeat_alert or scan_alert or (alert_type is not None)

    await Logging.record_security_incident(
        timestamp=ts,
        credential_id=payload.credential_id,
        actor_role=actor_role,
        target_id=target_label,
        action_type=AuditActionType.ACCESS_DENIAL,
        event_payload={
            "elevator_id": payload.elevator_id,
            "is_dashboard_access": payload.is_dashboard_access,
            "origin_floor": payload.origin_floor,
            "denial_reason": denial_reason.value,
        },
        elevator_id=car_id_or_none,
        origin_floor=payload.origin_floor,
        destination_floor=payload.origin_floor,
        denial_reason=denial_reason,
        alert_triggered=alert_fired,
        alert_severity=alert_severity,
        alert_type=alert_type,
        alert_details=alert_details,
        primary_car_id=car_id_or_none,
        db_pool=db_pool,
    )
    return None, AuthorizationResult.deny(
        denial_reason,
        message=message,
        credential_id=payload.credential_id,
        alert_triggered=alert_fired,
    )
