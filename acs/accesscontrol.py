from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Any, Optional
import uuid

import asyncpg

from acs.logs import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    DenialReason,
    IntegrityCheckResult,
    Logging,
)
from db import DbConnectionPool

ALL_BUILDING_FLOORS: list[int] = [-1] + list(range(1, 31))
COMMON_FLOORS: list[int] = [-1, 1]


# =============================================================================
# Enum Definitions
# =============================================================================


class ElevatorId(StrEnum):
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_

    @classmethod
    def is_residential(cls, value: Optional[str]) -> bool:
        return value in (cls.E1, cls.E2, cls.E3, cls.E4)


class UserRole(StrEnum):
    RESIDENT = "RESIDENT"
    VISITOR = "VISITOR"
    BUILDING_SERVICE = "BUILDING_SERVICE"
    TECHNICIAN = "TECHNICIAN"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"

    @classmethod
    def can_initiate_service_run(cls, role: str) -> bool:
        return role in (cls.BUILDING_SERVICE, cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def can_request_e5_freight(cls, role: str) -> bool:
        return role in (cls.RESIDENT, cls.BUILDING_SERVICE, cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def can_perform_maintenance(cls, role: str) -> bool:
        return role in (cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def is_admin(cls, role: str) -> bool:
        return role == cls.ADMIN


class CredentialType(StrEnum):
    RESIDENT = "RESIDENT"
    VISITOR = "VISITOR"
    STAFF = "STAFF"
    TECHNICIAN = "TECHNICIAN"
    ADMIN = "ADMIN"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class SuspensionReason(StrEnum):
    LOST = "LOST"
    CLONE_SUSPECTED = "CLONE_SUSPECTED"
    ADMIN_MANUAL = "ADMIN_MANUAL"


class MaintenanceOperationType(StrEnum):
    ENTER_MAINTENANCE = "ENTER_MAINTENANCE"
    DIAGNOSTIC_QUERY = "DIAGNOSTIC_QUERY"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


class EmergencyOverrideType(StrEnum):
    DOOR_OVERRIDE = "DOOR_OVERRIDE"
    MANUAL_RECALL = "MANUAL_RECALL"
    RESOLVE_INCIDENT = "RESOLVE_INCIDENT"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


class CredentialAction(StrEnum):
    ISSUE = "ISSUE"
    SUSPEND = "SUSPEND"
    REINSTATE = "REINSTATE"
    REVOKE = "REVOKE"
    ISSUE_VISITOR_PASS = "ISSUE_VISITOR_PASS"


ROLE_TO_CREDENTIAL_TYPE: dict[UserRole, CredentialType] = {
    UserRole.RESIDENT: CredentialType.RESIDENT,
    UserRole.VISITOR: CredentialType.VISITOR,
    UserRole.BUILDING_SERVICE: CredentialType.STAFF,
    UserRole.TECHNICIAN: CredentialType.TECHNICIAN,
    UserRole.ADMIN: CredentialType.ADMIN,
}


# =============================================================================
# Strongly-Typed OOP Payload & Verdict Classes (Section 4)
# =============================================================================


@dataclass
class AuthorizationResult:
    outcome: AuthorizationOutcome
    allowed: bool
    reason_code: Optional[DenialReason | str] = None
    message: str = ""
    session_token: Optional[str] = None
    credential_id: Optional[str] = None
    alert_triggered: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        *,
        message: str = "Access granted.",
        session_token: Optional[str] = None,
        credential_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuthorizationResult:
        return cls(
            outcome=AuthorizationOutcome.ALLOWED,
            allowed=True,
            reason_code=None,
            message=message,
            session_token=session_token,
            credential_id=credential_id,
            alert_triggered=False,
            details=details or {},
        )

    @classmethod
    def deny(
        cls,
        reason_code: DenialReason | str,
        *,
        message: str = "Access denied.",
        credential_id: Optional[str] = None,
        alert_triggered: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> AuthorizationResult:
        return cls(
            outcome=AuthorizationOutcome.DENIED,
            allowed=False,
            reason_code=reason_code,
            message=message,
            session_token=None,
            credential_id=credential_id,
            alert_triggered=alert_triggered,
            details=details or {},
        )


@dataclass
class CardSwipePayload:
    """4.1 Credential Presentation on In-Cabin Reader (`UI-8`)."""

    elevator_id: ElevatorId | str
    credential_id: str
    timestamp: int
    origin_floor: int = 1


@dataclass
class CarDestinationRequestPayload:
    """4.2 Passenger Floor Button Press inside an Elevator Cab."""

    elevator_id: ElevatorId | str
    origin_floor: int
    requested_floor: int
    timestamp: int


@dataclass
class E5ServiceRunPayload:
    """4.3 Dedicated Freight Run (`FR-1.13`)."""

    elevator_id: ElevatorId | str
    destination_floor: int
    purpose: str
    timestamp: int


@dataclass
class MaintenanceOperationPayload:
    """4.4 Technician Isolation & Diagnostics (`MNT-4.3`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    operation_type: MaintenanceOperationType | str
    timestamp: int


@dataclass
class ReturnToServicePayload:
    """4.5 Restoring Elevator to Service (`MNT-4.6`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    technician_confirmation: bool
    timestamp: int


@dataclass
class EmergencyOverridePayload:
    """4.6 Admin Safety Overrides (`EMR-3.8`)."""

    credential_id: str
    override_type: EmergencyOverrideType | str
    target_car_id: Optional[ElevatorId | str]
    incident_id: Optional[str]
    reason: str
    timestamp: int


@dataclass
class RescueCarDispatchPayload:
    """4.7 Rescue Car Dispatch (`EMR-3.12, EMR-3.16`)."""

    credential_id: str
    incident_id: str
    rescue_car_id: ElevatorId | str
    incident_floor: int
    dwell_wait_seconds: int = 120
    timestamp: int = 0


@dataclass
class CredentialManagementPayload:
    """4.8 Admin Credential Lifecycle Operations (`SEC-2.16`)."""

    admin_credential_id: str
    action: CredentialAction | str
    target_user_id: Optional[str]
    target_credential_id: Optional[str]
    payload_details: dict[str, Any] = field(default_factory=dict)
    timestamp: int = 0


@dataclass
class RevokeAndReissuePayload:
    """4.9 Atomic Lost Card Replacement (`SEC-2.17, VT-17`)."""

    admin_credential_id: str
    lost_credential_id: str
    replacement_credential_id: str
    reason: str = "LOST_AND_REPLACED"
    timestamp: int = 0


# =============================================================================
# Submodule 1: ACS.AccessControl (Section 3.1, 4, and 6)
# =============================================================================


class AccessControl:
    """
    ACS.AccessControl submodule (`acs/accesscontrol.py`).
    Validates authentication and role/floor authorization for SREMS operations,
    manages single-use in-cabin presentation tokens, enforces fail-closed policy,
    and delegates persistence & tamper-evident hash-chaining to `ACS.Logging`.
    """

    # Configurable simulation tick rate (default: 1 tick = 1 second)
    TICKS_PER_SECOND: int = 1

    # Policy time windows (in seconds, scaled by TICKS_PER_SECOND)
    RESIDENT_VISITOR_WINDOW_SECONDS: int = 5
    STAFF_TECH_WINDOW_SECONDS: int = 30
    CLONE_DETECTION_WINDOW_SECONDS: int = 10
    REPEATED_FAILURES_WINDOW_SECONDS: int = 60
    REPEATED_FAILURES_THRESHOLD: int = 3
    SCANNING_WINDOW_SECONDS: int = 300  # 5 minutes
    SCANNING_UNIQUE_CARDS_THRESHOLD: int = 11
    LOST_CARD_REVIEW_WINDOW_SECONDS: int = 72 * 3600  # 72 hours
    VISITOR_PASS_DEFAULT_DURATION_SECONDS: int = 4 * 3600  # 4 hours

    # Section 6: Operational In-Memory State
    # 1. car_id -> { session_token, credential_id, role, allowed_floors, granted_tick, expires_at_tick, consumed }
    active_car_sessions: dict[str, dict[str, Any]] = {}

    # 2. Rolling queue of { credential_id, car_id, tick_timestamp } for last 10s
    recent_presentations_window: list[dict[str, Any]] = []

    # 3. credential_id -> [failed tick timestamps in last 60s]
    failed_attempts_counter: dict[str, list[int]] = {}

    # 4. Sliding 5-minute log of (credential_id, tick_timestamp) for unknown card IDs
    unregistered_scan_counter: list[tuple[str, int]] = []

    @staticmethod
    def _seconds_to_ticks(seconds: int) -> int:
        return seconds * max(1, AccessControl.TICKS_PER_SECOND)

    @staticmethod
    def reset_in_memory_state() -> None:
        """Clear all in-memory session tokens and anomaly detection windows."""
        AccessControl.active_car_sessions.clear()
        AccessControl.recent_presentations_window.clear()
        AccessControl.failed_attempts_counter.clear()
        AccessControl.unregistered_scan_counter.clear()

    @staticmethod
    def _parse_allowed_floors(
        raw_floors: Any,
        role: str,
        assigned_floor: Optional[int] = None,
    ) -> list[int]:
        if UserRole.is_admin(role):
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

        # Common floors F1 (1) and B1 (-1) are granted to all registered credentials (Section 1.2)
        floor_set = set(floors) | set(COMMON_FLOORS)
        if role == UserRole.RESIDENT and assigned_floor is not None:
            floor_set.add(int(assigned_floor))

        return sorted(floor_set)

    @staticmethod
    async def _record_failed_attempt(
        *,
        credential_id: str,
        car_id: Optional[str],
        timestamp: int,
        reason: str,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        """
        Increment the 60-second sliding window of failed attempts for `credential_id` (`SEC-2.7`).
        Triggers a `REPEATED_FAILURES` security alert when >= 3 attempts occur within 60s.
        """
        window_ticks = AccessControl._seconds_to_ticks(
            AccessControl.REPEATED_FAILURES_WINDOW_SECONDS
        )
        cutoff = timestamp - window_ticks
        history = [
            t for t in AccessControl.failed_attempts_counter.get(credential_id, []) if t >= cutoff
        ]
        history.append(timestamp)
        AccessControl.failed_attempts_counter[credential_id] = history

        if len(history) >= AccessControl.REPEATED_FAILURES_THRESHOLD:
            await Logging.create_security_alert(
                severity=AlertSeverity.MEDIUM,
                alert_type=AlertType.REPEATED_FAILURES,
                credential_id=credential_id,
                primary_car_id=car_id if ElevatorId.has_value(car_id) else None,
                details=(
                    f"Credential '{credential_id}' accumulated {len(history)} failed attempts "
                    f"within {AccessControl.REPEATED_FAILURES_WINDOW_SECONDS}s (latest reason: {reason})."
                ),
                created_at=timestamp,
                db_pool=db_pool,
            )
            return True
        return False

    @staticmethod
    async def _record_unregistered_scan(
        *,
        credential_id: str,
        car_id: Optional[str],
        timestamp: int,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        """
        Track distinct unknown credentials over a 5-minute sliding window (`SEC-2.19, VT-32`).
        Triggers a `CARD_SCANNING` alert when >= 11 distinct unregistered IDs are presented.
        """
        window_ticks = AccessControl._seconds_to_ticks(AccessControl.SCANNING_WINDOW_SECONDS)
        cutoff = timestamp - window_ticks
        AccessControl.unregistered_scan_counter = [
            (cid, t) for cid, t in AccessControl.unregistered_scan_counter if t >= cutoff
        ]
        AccessControl.unregistered_scan_counter.append((credential_id, timestamp))

        unique_ids = {cid for cid, _ in AccessControl.unregistered_scan_counter}
        if len(unique_ids) >= AccessControl.SCANNING_UNIQUE_CARDS_THRESHOLD:
            await Logging.create_security_alert(
                severity=AlertSeverity.HIGH,
                alert_type=AlertType.CARD_SCANNING,
                credential_id=credential_id,
                primary_car_id=car_id if ElevatorId.has_value(car_id) else None,
                details=(
                    f"Card scanning brute-force detected: {len(unique_ids)} distinct unregistered "
                    f"credential IDs presented within {AccessControl.SCANNING_WINDOW_SECONDS}s."
                ),
                created_at=timestamp,
                db_pool=db_pool,
            )
            return True
        return False

    @staticmethod
    async def _fetch_and_refresh_credential(
        conn: asyncpg.Connection,
        credential_id: str,
        current_tick: int,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> Optional[dict[str, Any]]:
        """
        Fetch a credential joined with its user record and apply automatic tick-based
        state transitions (4h visitor pass expiration `SEC-2.22` and 72h lost-card revocation `SEC-2.17`).
        """
        row = await conn.fetchrow(
            """
            SELECT
                c.credential_id,
                c.user_id,
                c.credential_type,
                c.status,
                c.suspension_reason,
                c.suspension_start,
                c.revocation_reason,
                c.revoked_at,
                c.expires_at,
                c.allowed_floors,
                c.issued_by_admin,
                c.created_at,
                c.updated_at,
                u.full_name,
                u.role AS user_role,
                u.apartment_unit,
                u.assigned_floor
            FROM credentials c
            JOIN users u ON c.user_id = u.user_id
            WHERE c.credential_id = $1
            """,
            credential_id,
        )
        if row is None:
            return None

        record = dict(row)
        status = record["status"]

        # 1. Check Visitor Pass / Time-Limited Credential Expiration (SEC-2.22, VT-31)
        if (
            status == CredentialStatus.ACTIVE
            and record["expires_at"] is not None
            and current_tick >= int(record["expires_at"])
        ):
            await conn.execute(
                """
                UPDATE credentials
                SET status = $2, updated_at = $3
                WHERE credential_id = $1
                """,
                credential_id,
                CredentialStatus.EXPIRED.value,
                current_tick,
            )
            record["status"] = CredentialStatus.EXPIRED.value
            record["updated_at"] = current_tick
            await Logging.write_audit(
                timestamp=current_tick,
                actor_id=UserRole.SYSTEM,
                actor_role=UserRole.SYSTEM,
                action_type=AuditActionType.VISITOR_PASS_EXPIRED,
                target_id=credential_id,
                event_payload={
                    "credential_id": credential_id,
                    "expires_at": int(record["expires_at"]),
                    "total_valid_duration": int(record["expires_at"]) - int(record["created_at"]),
                    "final_status": CredentialStatus.EXPIRED.value,
                },
                db_pool=db_pool,
            )

        # 2. Check 72-Hour Lost Card Review Window Auto-Revocation (SEC-2.17)
        elif (
            status == CredentialStatus.SUSPENDED
            and record["suspension_reason"] == SuspensionReason.LOST
            and record["suspension_start"] is not None
        ):
            review_window_ticks = AccessControl._seconds_to_ticks(
                AccessControl.LOST_CARD_REVIEW_WINDOW_SECONDS
            )
            if current_tick - int(record["suspension_start"]) >= review_window_ticks:
                await conn.execute(
                    """
                    UPDATE credentials
                    SET status = $2,
                        revocation_reason = 'WINDOW_EXPIRED',
                        revoked_at = $3,
                        updated_at = $3
                    WHERE credential_id = $1
                    """,
                    credential_id,
                    CredentialStatus.REVOKED.value,
                    current_tick,
                )
                record["status"] = CredentialStatus.REVOKED.value
                record["revocation_reason"] = "WINDOW_EXPIRED"
                record["revoked_at"] = current_tick
                record["updated_at"] = current_tick
                await Logging.write_audit(
                    timestamp=current_tick,
                    actor_id=UserRole.SYSTEM,
                    actor_role=UserRole.SYSTEM,
                    action_type=AuditActionType.CREDENTIAL_REVOKED,
                    target_id=credential_id,
                    event_payload={
                        "credential_id": credential_id,
                        "trigger": "WINDOW_EXPIRED",
                        "suspension_start": int(record["suspension_start"]),
                        "revoked_at": current_tick,
                    },
                    db_pool=db_pool,
                )

        return record

    # -------------------------------------------------------------------------
    # 4.1 CardSwipePayload Validation (`SEC-2.16, SEC-2.17, SEC-2.18, SEC-2.19, SEC-2.21`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_card_swipe(
        payload: CardSwipePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        if not ElevatorId.has_value(payload.elevator_id):
            try:
                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=UserRole.SYSTEM,
                    actor_role=UserRole.SYSTEM,
                    action_type=AuditActionType.MALFORMED_MSG_REJECTED,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "source": "CardSwipePayload",
                        "elevator_id": str(payload.elevator_id),
                        "credential_id": payload.credential_id,
                        "violation": "INVALID_ELEVATOR_ID",
                    },
                    db_pool=db_pool,
                )
            except Exception:
                return AuthorizationResult.deny(
                    DenialReason.DB_FAIL_CLOSED,
                    message="Database error: fail-closed policy enforced.",
                    credential_id=payload.credential_id,
                )
            return AuthorizationResult.deny(
                DenialReason.MALFORMED_MSG_REJECTED,
                message=f"Unknown elevator_id '{payload.elevator_id}'.",
                credential_id=payload.credential_id,
            )

        try:
            async with db_pool.transaction() as conn:
                # Step 1: Cloned Card Check (< 10 seconds across different cars, SEC-2.18)
                clone_window_ticks = AccessControl._seconds_to_ticks(
                    AccessControl.CLONE_DETECTION_WINDOW_SECONDS
                )
                AccessControl.recent_presentations_window = [
                    entry
                    for entry in AccessControl.recent_presentations_window
                    if (payload.timestamp - entry["tick_timestamp"]) < clone_window_ticks
                ]

                conflicting_entry = next(
                    (
                        entry
                        for entry in AccessControl.recent_presentations_window
                        if entry["credential_id"] == payload.credential_id
                        and entry["car_id"] != payload.elevator_id
                        and 0 <= (payload.timestamp - entry["tick_timestamp"]) < clone_window_ticks
                    ),
                    None,
                )

                AccessControl.recent_presentations_window.append(
                    {
                        "credential_id": payload.credential_id,
                        "car_id": str(payload.elevator_id),
                        "tick_timestamp": payload.timestamp,
                    }
                )

                if conflicting_entry is not None:
                    prior_car = conflicting_entry["car_id"]
                    prior_session = AccessControl.active_car_sessions.get(prior_car)
                    if prior_session and prior_session.get("credential_id") == payload.credential_id:
                        AccessControl.active_car_sessions.pop(prior_car, None)
                    AccessControl.active_car_sessions.pop(str(payload.elevator_id), None)

                    cred_row = await conn.fetchrow(
                        "SELECT credential_id FROM credentials WHERE credential_id = $1",
                        payload.credential_id,
                    )
                    if cred_row is not None:
                        await conn.execute(
                            """
                            UPDATE credentials
                            SET status = $2,
                                suspension_reason = $3,
                                suspension_start = $4,
                                updated_at = $4
                            WHERE credential_id = $1
                            """,
                            payload.credential_id,
                            CredentialStatus.SUSPENDED.value,
                            SuspensionReason.CLONE_SUSPECTED.value,
                            payload.timestamp,
                        )
                        await Logging.write_audit(
                            timestamp=payload.timestamp,
                            actor_id=UserRole.SYSTEM,
                            actor_role=UserRole.SYSTEM,
                            action_type=AuditActionType.CREDENTIAL_SUSPENDED,
                            target_id=payload.credential_id,
                            event_payload={
                                "credential_id": payload.credential_id,
                                "reason": SuspensionReason.CLONE_SUSPECTED.value,
                                "primary_car_id": str(payload.elevator_id),
                                "secondary_car_id": prior_car,
                            },
                            db_pool=db_pool,
                        )

                    await Logging.create_security_alert(
                        severity=AlertSeverity.CRITICAL,
                        alert_type=AlertType.CLONED_CREDENTIAL,
                        credential_id=payload.credential_id,
                        primary_car_id=str(payload.elevator_id),
                        secondary_car_id=prior_car,
                        details=(
                            f"Cloned credential '{payload.credential_id}' tapped in {payload.elevator_id} "
                            f"and {prior_car} within < {AccessControl.CLONE_DETECTION_WINDOW_SECONDS}s."
                        ),
                        created_at=payload.timestamp,
                        db_pool=db_pool,
                    )
                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=payload.credential_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.origin_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.CARD_SUSPENDED,
                        alert_triggered=True,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "secondary_car_id": prior_car,
                            "origin_floor": payload.origin_floor,
                            "denial_reason": SuspensionReason.CLONE_SUSPECTED.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.CARD_SUSPENDED,
                        message=f"Cloned credential detected across {prior_car} and {payload.elevator_id}; card suspended.",
                        credential_id=payload.credential_id,
                        alert_triggered=True,
                        details={
                            "secondary_car_id": prior_car,
                            "suspension_reason": SuspensionReason.CLONE_SUSPECTED.value,
                        },
                    )

                # Step 2: Credential Registration & Status Lookup (SEC-2.16, SEC-2.19)
                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, payload.timestamp, db_pool=db_pool
                )

                if record is None:
                    repeat_alert = await AccessControl._record_failed_attempt(
                        credential_id=payload.credential_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        reason=DenialReason.UNREGISTERED_CARD,
                        db_pool=db_pool,
                    )
                    scan_alert = await AccessControl._record_unregistered_scan(
                        credential_id=payload.credential_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        db_pool=db_pool,
                    )
                    alert_fired = repeat_alert or scan_alert

                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=payload.credential_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.origin_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.UNREGISTERED_CARD,
                        alert_triggered=alert_fired,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "origin_floor": payload.origin_floor,
                            "denial_reason": DenialReason.UNREGISTERED_CARD.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message="Unregistered credential presented.",
                        credential_id=payload.credential_id,
                        alert_triggered=alert_fired,
                    )

                status = record["status"]
                user_role = record["user_role"]

                # Step 2b: Suspended, Revoked, or Expired Credential Handling (SEC-2.17)
                if status in (
                    CredentialStatus.SUSPENDED,
                    CredentialStatus.REVOKED,
                    CredentialStatus.EXPIRED,
                ):
                    alert_fired = False
                    if status == CredentialStatus.SUSPENDED:
                        denial_reason = DenialReason.CARD_SUSPENDED
                        alert_type = (
                            AlertType.CLONED_CREDENTIAL
                            if record.get("suspension_reason") == SuspensionReason.CLONE_SUSPECTED
                            else AlertType.LOST_CARD_USAGE
                        )
                        await Logging.create_security_alert(
                            severity=AlertSeverity.HIGH,
                            alert_type=alert_type,
                            credential_id=payload.credential_id,
                            primary_car_id=str(payload.elevator_id),
                            details=(
                                f"Suspended credential '{payload.credential_id}' "
                                f"(reason: {record.get('suspension_reason')}) tapped in {payload.elevator_id}."
                            ),
                            created_at=payload.timestamp,
                            db_pool=db_pool,
                        )
                        alert_fired = True
                    elif status == CredentialStatus.REVOKED:
                        denial_reason = DenialReason.CARD_REVOKED
                        await Logging.create_security_alert(
                            severity=AlertSeverity.HIGH,
                            alert_type=AlertType.REVOKED_CARD_USAGE,
                            credential_id=payload.credential_id,
                            primary_car_id=str(payload.elevator_id),
                            details=f"Revoked credential '{payload.credential_id}' tapped in {payload.elevator_id}.",
                            created_at=payload.timestamp,
                            db_pool=db_pool,
                        )
                        alert_fired = True
                    else:
                        denial_reason = DenialReason.CARD_EXPIRED

                    repeat_alert = await AccessControl._record_failed_attempt(
                        credential_id=payload.credential_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        reason=denial_reason,
                        db_pool=db_pool,
                    )
                    alert_fired = alert_fired or repeat_alert

                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=payload.credential_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.origin_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=denial_reason,
                        alert_triggered=alert_fired,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=user_role,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "origin_floor": payload.origin_floor,
                            "denial_reason": denial_reason.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        denial_reason,
                        message=f"Credential is {status}.",
                        credential_id=payload.credential_id,
                        alert_triggered=alert_fired,
                    )

                # Step 3 & 4: Role & Floor Extraction + Generate Per-Car Session Token (SEC-2.21)
                allowed_floors = AccessControl._parse_allowed_floors(
                    record["allowed_floors"],
                    role=user_role,
                    assigned_floor=record.get("assigned_floor"),
                )
                timeout_seconds = (
                    AccessControl.STAFF_TECH_WINDOW_SECONDS
                    if user_role in (UserRole.BUILDING_SERVICE, UserRole.TECHNICIAN)
                    else AccessControl.RESIDENT_VISITOR_WINDOW_SECONDS
                )
                session_token = str(uuid.uuid4())
                expires_at_tick = payload.timestamp + AccessControl._seconds_to_ticks(
                    timeout_seconds
                )

                AccessControl.active_car_sessions[str(payload.elevator_id)] = {
                    "session_token": session_token,
                    "credential_id": payload.credential_id,
                    "user_id": record["user_id"],
                    "role": user_role,
                    "allowed_floors": allowed_floors,
                    "granted_tick": payload.timestamp,
                    "expires_at_tick": expires_at_tick,
                    "consumed": False,
                }

                return AuthorizationResult.allow(
                    message="Card verified; destination selection window active.",
                    session_token=session_token,
                    credential_id=payload.credential_id,
                    details={
                        "elevator_id": str(payload.elevator_id),
                        "role": user_role,
                        "allowed_floors": allowed_floors,
                        "expires_at_tick": expires_at_tick,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.2 CarDestinationRequestPayload Validation (`SEC-2.5, SEC-2.6, SEC-2.21`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_car_destination(
        payload: CarDestinationRequestPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        if not ElevatorId.has_value(payload.elevator_id) or not (
            payload.requested_floor == -1 or (1 <= payload.requested_floor <= 30)
        ):
            try:
                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=UserRole.SYSTEM,
                    actor_role=UserRole.SYSTEM,
                    action_type=AuditActionType.MALFORMED_MSG_REJECTED,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "source": "CarDestinationRequestPayload",
                        "elevator_id": str(payload.elevator_id),
                        "origin_floor": payload.origin_floor,
                        "requested_floor": payload.requested_floor,
                    },
                    db_pool=db_pool,
                )
            except Exception:
                return AuthorizationResult.deny(
                    DenialReason.DB_FAIL_CLOSED,
                    message="Database error: fail-closed policy enforced.",
                )
            return AuthorizationResult.deny(
                DenialReason.MALFORMED_MSG_REJECTED,
                message="Invalid elevator_id or requested_floor.",
            )

        try:
            async with db_pool.transaction() as conn:
                # Step 1: B1 Universal Exemption (SEC-2.5) & E5 B1 Prohibition (FR-1.10)
                if payload.requested_floor == -1:
                    if payload.elevator_id == ElevatorId.E5:
                        session = AccessControl.active_car_sessions.get(ElevatorId.E5.value)
                        cred_id = session["credential_id"] if session else None
                        actor_role = session["role"] if session else UserRole.SYSTEM
                        await Logging.log_authorization_event(
                            timestamp=payload.timestamp,
                            credential_id=cred_id,
                            elevator_id=str(payload.elevator_id),
                            origin_floor=payload.origin_floor,
                            destination_floor=payload.requested_floor,
                            outcome=AuthorizationOutcome.DENIED,
                            denial_reason=DenialReason.E5_B1_FORBIDDEN,
                            alert_triggered=False,
                            db_pool=db_pool,
                        )
                        await Logging.write_audit(
                            timestamp=payload.timestamp,
                            actor_id=cred_id or UserRole.SYSTEM,
                            actor_role=actor_role,
                            action_type=AuditActionType.ACCESS_DENIAL,
                            target_id=str(payload.elevator_id),
                            event_payload={
                                "elevator_id": str(payload.elevator_id),
                                "origin_floor": payload.origin_floor,
                                "requested_floor": payload.requested_floor,
                                "denial_reason": DenialReason.E5_B1_FORBIDDEN.value,
                            },
                            db_pool=db_pool,
                        )
                        return AuthorizationResult.deny(
                            DenialReason.E5_B1_FORBIDDEN,
                            message="Freight elevator E5 does not serve Basement 1 (B1).",
                            credential_id=cred_id,
                        )

                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=None,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.requested_floor,
                        outcome=AuthorizationOutcome.ALLOWED,
                        denial_reason=None,
                        alert_triggered=False,
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.allow(
                        message="Basement 1 (B1) is universally accessible on residential cars.",
                        details={
                            "elevator_id": str(payload.elevator_id),
                            "requested_floor": -1,
                        },
                    )

                # Step 2: Session Token Lookup (SEC-2.6, SEC-2.21)
                session = AccessControl.active_car_sessions.get(str(payload.elevator_id))
                if session is None:
                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=None,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.requested_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.NO_ACTIVE_CREDENTIAL,
                        alert_triggered=False,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=UserRole.SYSTEM,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "origin_floor": payload.origin_floor,
                            "requested_floor": payload.requested_floor,
                            "denial_reason": DenialReason.NO_ACTIVE_CREDENTIAL.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.NO_ACTIVE_CREDENTIAL,
                        message="No active credential session in this elevator.",
                    )

                cred_id = session["credential_id"]
                role = session["role"]

                if session.get("consumed", False):
                    alert_fired = await AccessControl._record_failed_attempt(
                        credential_id=cred_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        reason=DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        db_pool=db_pool,
                    )
                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=cred_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.requested_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        alert_triggered=alert_fired,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.ONE_TAP_VIOLATION,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "denied_floor": payload.requested_floor,
                            "token_age": payload.timestamp - int(session["granted_tick"]),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        message="Single-use tap token already consumed; please re-tap credential.",
                        credential_id=cred_id,
                        alert_triggered=alert_fired,
                    )

                if payload.timestamp > int(session["expires_at_tick"]):
                    alert_fired = await AccessControl._record_failed_attempt(
                        credential_id=cred_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        reason=DenialReason.TOKEN_WINDOW_EXPIRED,
                        db_pool=db_pool,
                    )
                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=cred_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.requested_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.TOKEN_WINDOW_EXPIRED,
                        alert_triggered=alert_fired,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "origin_floor": payload.origin_floor,
                            "requested_floor": payload.requested_floor,
                            "denial_reason": DenialReason.TOKEN_WINDOW_EXPIRED.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.TOKEN_WINDOW_EXPIRED,
                        message="Credential selection window expired; please re-tap credential.",
                        credential_id=cred_id,
                        alert_triggered=alert_fired,
                    )

                # Step 3: Floor & E5 Role Authorization Check (Section 1.2 & 4.2)
                e5_forbidden = (
                    payload.elevator_id == ElevatorId.E5
                    and not UserRole.can_request_e5_freight(role)
                )
                floor_forbidden = payload.requested_floor not in session["allowed_floors"]

                if e5_forbidden or floor_forbidden:
                    alert_fired = await AccessControl._record_failed_attempt(
                        credential_id=cred_id,
                        car_id=str(payload.elevator_id),
                        timestamp=payload.timestamp,
                        reason=DenialReason.UNAUTHORIZED_FLOOR,
                        db_pool=db_pool,
                    )
                    await Logging.log_authorization_event(
                        timestamp=payload.timestamp,
                        credential_id=cred_id,
                        elevator_id=str(payload.elevator_id),
                        origin_floor=payload.origin_floor,
                        destination_floor=payload.requested_floor,
                        outcome=AuthorizationOutcome.DENIED,
                        denial_reason=DenialReason.UNAUTHORIZED_FLOOR,
                        alert_triggered=alert_fired,
                        db_pool=db_pool,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.ACCESS_DENIAL,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "origin_floor": payload.origin_floor,
                            "requested_floor": payload.requested_floor,
                            "denial_reason": DenialReason.UNAUTHORIZED_FLOOR.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_FLOOR,
                        message=f"Credential '{cred_id}' is not authorized for floor {payload.requested_floor} on {payload.elevator_id}.",
                        credential_id=cred_id,
                        alert_triggered=alert_fired,
                    )

                # Step 4: Consume Token (SEC-2.21)
                session["consumed"] = True
                await Logging.log_authorization_event(
                    timestamp=payload.timestamp,
                    credential_id=cred_id,
                    elevator_id=str(payload.elevator_id),
                    origin_floor=payload.origin_floor,
                    destination_floor=payload.requested_floor,
                    outcome=AuthorizationOutcome.ALLOWED,
                    denial_reason=None,
                    alert_triggered=False,
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Destination floor {payload.requested_floor} authorized for {payload.elevator_id}.",
                    session_token=session["session_token"],
                    credential_id=cred_id,
                    details={
                        "elevator_id": str(payload.elevator_id),
                        "requested_floor": payload.requested_floor,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
            )

    # -------------------------------------------------------------------------
    # 4.3 E5ServiceRunPayload Validation (`FR-1.13`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_e5_service_run(
        payload: E5ServiceRunPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                if payload.elevator_id != ElevatorId.E5 or not (
                    1 <= payload.destination_floor <= 30
                ):
                    reason = (
                        DenialReason.E5_B1_FORBIDDEN
                        if payload.destination_floor == -1
                        else DenialReason.MALFORMED_MSG_REJECTED
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=UserRole.SYSTEM,
                        actor_role=UserRole.SYSTEM,
                        action_type=(
                            AuditActionType.MALFORMED_MSG_REJECTED
                            if reason == DenialReason.MALFORMED_MSG_REJECTED
                            else AuditActionType.ACCESS_DENIAL
                        ),
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "elevator_id": str(payload.elevator_id),
                            "destination_floor": payload.destination_floor,
                            "purpose": payload.purpose,
                            "reason": reason.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="E5 service run requires elevator_id='E5' and destination_floor in 1..30.",
                    )

                session = AccessControl.active_car_sessions.get(ElevatorId.E5.value)
                if session is None:
                    return AuthorizationResult.deny(
                        DenialReason.NO_ACTIVE_CREDENTIAL,
                        message="No active credential session in E5.",
                    )

                cred_id = session["credential_id"]
                role = session["role"]

                if session.get("consumed", False):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.ONE_TAP_VIOLATION,
                        target_id=ElevatorId.E5,
                        event_payload={
                            "elevator_id": ElevatorId.E5.value,
                            "denied_floor": payload.destination_floor,
                            "token_age": payload.timestamp - int(session["granted_tick"]),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        message="Session token already consumed.",
                        credential_id=cred_id,
                    )

                if payload.timestamp > int(session["expires_at_tick"]):
                    return AuthorizationResult.deny(
                        DenialReason.TOKEN_WINDOW_EXPIRED,
                        message="Session token expired.",
                        credential_id=cred_id,
                    )

                if not UserRole.can_initiate_service_run(role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=ElevatorId.E5,
                        event_payload={
                            "attempted_operation": AuditActionType.E5_SERVICE_RUN_START.value,
                            "destination_floor": payload.destination_floor,
                            "purpose": payload.purpose,
                            "missing_required_role": [
                                UserRole.BUILDING_SERVICE.value,
                                UserRole.TECHNICIAN.value,
                                UserRole.ADMIN.value,
                            ],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{role}' is not authorized to initiate an E5 service run.",
                        credential_id=cred_id,
                    )

                session["consumed"] = True
                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=cred_id,
                    actor_role=role,
                    action_type=AuditActionType.E5_SERVICE_RUN_START,
                    target_id=ElevatorId.E5,
                    event_payload={
                        "elevator_id": ElevatorId.E5.value,
                        "destination_floor": payload.destination_floor,
                        "purpose": payload.purpose,
                    },
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message="E5 dedicated service run authorized.",
                    session_token=session["session_token"],
                    credential_id=cred_id,
                    details={
                        "elevator_id": ElevatorId.E5.value,
                        "destination_floor": payload.destination_floor,
                        "purpose": payload.purpose,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
            )

    # -------------------------------------------------------------------------
    # 4.4 MaintenanceOperationPayload Validation (`MNT-4.3`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_maintenance_operation(
        payload: MaintenanceOperationPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(
                    payload.elevator_id
                ) or not MaintenanceOperationType.has_value(payload.operation_type):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=UserRole.SYSTEM,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.MALFORMED_MSG_REJECTED,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "source": "MaintenanceOperationPayload",
                            "elevator_id": str(payload.elevator_id),
                            "operation_type": str(payload.operation_type),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message="Invalid elevator_id or operation_type.",
                        credential_id=payload.credential_id,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, payload.timestamp, db_pool=db_pool
                )
                if record is None or record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if record is None
                        else DenialReason(f"CARD_{record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active technician or admin credential required.",
                        credential_id=payload.credential_id,
                    )

                role = record["user_role"]
                if not UserRole.can_perform_maintenance(role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "attempted_operation": str(payload.operation_type),
                            "missing_required_role": [
                                UserRole.TECHNICIAN.value,
                                UserRole.ADMIN.value,
                            ],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{role}' cannot execute maintenance operation '{payload.operation_type}'.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=AuditActionType.MAINTENANCE_ENTER,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "elevator_id": str(payload.elevator_id),
                        "operation_type": str(payload.operation_type),
                    },
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Maintenance operation '{payload.operation_type}' authorized for {payload.elevator_id}.",
                    credential_id=payload.credential_id,
                    details={
                        "elevator_id": str(payload.elevator_id),
                        "operation_type": str(payload.operation_type),
                        "role": role,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.5 ReturnToServicePayload Validation (`MNT-4.6`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_return_to_service(
        payload: ReturnToServicePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(payload.elevator_id):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=UserRole.SYSTEM,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.MALFORMED_MSG_REJECTED,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "source": "ReturnToServicePayload",
                            "elevator_id": str(payload.elevator_id),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message="Invalid elevator_id.",
                        credential_id=payload.credential_id,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, payload.timestamp, db_pool=db_pool
                )
                if record is None or record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if record is None
                        else DenialReason(f"CARD_{record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active technician or admin credential required.",
                        credential_id=payload.credential_id,
                    )

                role = record["user_role"]
                if not UserRole.can_perform_maintenance(role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=str(payload.elevator_id),
                        event_payload={
                            "attempted_operation": AuditActionType.RETURN_TO_SERVICE.value,
                            "missing_required_role": [
                                UserRole.TECHNICIAN.value,
                                UserRole.ADMIN.value,
                            ],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{role}' is not authorized to return car to service.",
                        credential_id=payload.credential_id,
                    )

                if not payload.technician_confirmation:
                    return AuthorizationResult.deny(
                        DenialReason.CONFIRMATION_REQUIRED,
                        message="Technician safety confirmation is required to return elevator to service.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=AuditActionType.RETURN_TO_SERVICE,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "elevator_id": str(payload.elevator_id),
                        "technician_confirmation": payload.technician_confirmation,
                    },
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Return to service authorized for {payload.elevator_id}.",
                    credential_id=payload.credential_id,
                    details={"elevator_id": str(payload.elevator_id), "role": role},
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.6 EmergencyOverridePayload Validation (`EMR-3.8`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_emergency_override(
        payload: EmergencyOverridePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                if not EmergencyOverrideType.has_value(payload.override_type):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=UserRole.SYSTEM,
                        actor_role=UserRole.SYSTEM,
                        action_type=AuditActionType.MALFORMED_MSG_REJECTED,
                        target_id=str(payload.target_car_id or payload.incident_id or "UNKNOWN"),
                        event_payload={
                            "source": "EmergencyOverridePayload",
                            "override_type": str(payload.override_type),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message=f"Invalid override_type '{payload.override_type}'.",
                        credential_id=payload.credential_id,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, payload.timestamp, db_pool=db_pool
                )
                if record is None or record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if record is None
                        else DenialReason(f"CARD_{record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active Admin credential required.",
                        credential_id=payload.credential_id,
                    )

                role = record["user_role"]
                target_entity = str(payload.target_car_id or payload.incident_id or UserRole.SYSTEM)
                if not UserRole.is_admin(role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=target_entity,
                        event_payload={
                            "attempted_operation": str(payload.override_type),
                            "missing_required_role": [UserRole.ADMIN.value],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{role}' cannot execute emergency override '{payload.override_type}'.",
                        credential_id=payload.credential_id,
                    )

                audit_action = (
                    AuditActionType.INCIDENT_RESOLVED
                    if payload.override_type == EmergencyOverrideType.RESOLVE_INCIDENT
                    else AuditActionType.EMERGENCY_OVERRIDE
                )
                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=audit_action,
                    target_id=target_entity,
                    event_payload={
                        "override_type": str(payload.override_type),
                        "target_car_id": str(payload.target_car_id) if payload.target_car_id else None,
                        "incident_id": payload.incident_id,
                        "reason": payload.reason,
                    },
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Emergency override '{payload.override_type}' authorized.",
                    credential_id=payload.credential_id,
                    details={
                        "override_type": str(payload.override_type),
                        "target_car_id": str(payload.target_car_id) if payload.target_car_id else None,
                        "incident_id": payload.incident_id,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.7 RescueCarDispatchPayload Validation (`EMR-3.12, EMR-3.16`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_rescue_car_dispatch(
        payload: RescueCarDispatchPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, payload.timestamp, db_pool=db_pool
                )
                if record is None or record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if record is None
                        else DenialReason(f"CARD_{record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active Admin credential required.",
                        credential_id=payload.credential_id,
                    )

                role = record["user_role"]
                if not UserRole.is_admin(role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=str(payload.rescue_car_id),
                        event_payload={
                            "attempted_operation": AuditActionType.RESCUE_CAR_DISPATCHED.value,
                            "incident_id": payload.incident_id,
                            "missing_required_role": [UserRole.ADMIN.value],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{role}' is not authorized to dispatch a rescue car.",
                        credential_id=payload.credential_id,
                    )

                # E5 is never eligible as a rescue car (EMR-3.12, OR-15)
                if not ElevatorId.is_residential(payload.rescue_car_id):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        action_type=AuditActionType.COMMAND_CONFLICT_REJECTED,
                        target_id=str(payload.rescue_car_id),
                        event_payload={
                            "attempted_command": AuditActionType.RESCUE_CAR_DISPATCHED.value,
                            "rescue_car_id": str(payload.rescue_car_id),
                            "incident_id": payload.incident_id,
                            "conflict_reason": "E5_INELIGIBLE_AS_RESCUE_CAR",
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.COMMAND_CONFLICT_REJECTED,
                        message="Rescue car must be an eligible residential car (E1-E4); E5 cannot be used.",
                        credential_id=payload.credential_id,
                    )

                if not (1 <= payload.dwell_wait_seconds <= 600):
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message="dwell_wait_seconds must be between 1 and 600 seconds.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=AuditActionType.RESCUE_CAR_DISPATCHED,
                    target_id=str(payload.rescue_car_id),
                    event_payload={
                        "rescue_car_id": str(payload.rescue_car_id),
                        "incident_id": payload.incident_id,
                        "incident_floor": payload.incident_floor,
                        "dwell_wait_seconds": payload.dwell_wait_seconds,
                    },
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Rescue car {payload.rescue_car_id} dispatch authorized for incident {payload.incident_id}.",
                    credential_id=payload.credential_id,
                    details={
                        "rescue_car_id": str(payload.rescue_car_id),
                        "incident_id": payload.incident_id,
                        "incident_floor": payload.incident_floor,
                        "dwell_wait_seconds": payload.dwell_wait_seconds,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.8 CredentialManagementPayload Validation (`SEC-2.16, SEC-2.17, SEC-2.22`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def manage_credential(
        payload: CredentialManagementPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        try:
            async with db_pool.transaction() as conn:
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.admin_credential_id, payload.timestamp, db_pool=db_pool
                )
                if admin_record is None or admin_record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if admin_record is None
                        else DenialReason(f"CARD_{admin_record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active Admin credential required.",
                        credential_id=payload.admin_credential_id,
                    )

                admin_role = admin_record["user_role"]
                admin_user_id = admin_record["user_id"]
                if not UserRole.is_admin(admin_role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=payload.target_credential_id or payload.target_user_id or "UNKNOWN",
                        event_payload={
                            "attempted_operation": str(payload.action),
                            "missing_required_role": [UserRole.ADMIN.value],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{admin_role}' is not authorized to manage credentials.",
                        credential_id=payload.admin_credential_id,
                    )

                details = payload.payload_details or {}
                action = payload.action

                # --- Action: ISSUE or ISSUE_VISITOR_PASS ---
                if action in (CredentialAction.ISSUE, CredentialAction.ISSUE_VISITOR_PASS):
                    if not payload.target_credential_id:
                        return AuthorizationResult.deny(
                            DenialReason.MALFORMED_MSG_REJECTED,
                            message="target_credential_id is required when issuing a credential.",
                            credential_id=payload.admin_credential_id,
                        )

                    existing_cred = await conn.fetchrow(
                        "SELECT credential_id FROM credentials WHERE credential_id = $1",
                        payload.target_credential_id,
                    )
                    if existing_cred is not None:
                        return AuthorizationResult.deny(
                            DenialReason.CREDENTIAL_ALREADY_EXISTS,
                            message=f"Credential '{payload.target_credential_id}' already exists.",
                            credential_id=payload.admin_credential_id,
                        )

                    target_user_id = payload.target_user_id or str(uuid.uuid4())
                    user_row = await conn.fetchrow(
                        "SELECT user_id, role, assigned_floor FROM users WHERE user_id = $1",
                        target_user_id,
                    )

                    default_role = (
                        UserRole.VISITOR
                        if action == CredentialAction.ISSUE_VISITOR_PASS
                        else UserRole.RESIDENT
                    )
                    target_role_raw = details.get(
                        "role", user_row["role"] if user_row else default_role
                    )
                    target_role = (
                        UserRole.VISITOR.value
                        if action == CredentialAction.ISSUE_VISITOR_PASS
                        else str(target_role_raw)
                    )

                    assigned_floor = details.get(
                        "assigned_floor", user_row["assigned_floor"] if user_row else None
                    )
                    if user_row is None:
                        full_name = details.get(
                            "full_name", f"{target_role.title()} {target_user_id[:8]}"
                        )
                        apartment_unit = details.get("apartment_unit")
                        await conn.execute(
                            """
                            INSERT INTO users (
                                user_id, full_name, role, apartment_unit, assigned_floor, created_at, updated_at
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $6)
                            """,
                            target_user_id,
                            full_name,
                            target_role,
                            apartment_unit,
                            assigned_floor,
                            payload.timestamp,
                        )

                    allowed_floors = AccessControl._parse_allowed_floors(
                        details.get("allowed_floors", []),
                        role=target_role,
                        assigned_floor=assigned_floor,
                    )
                    default_cred_type = ROLE_TO_CREDENTIAL_TYPE.get(
                        UserRole(target_role)
                        if target_role in UserRole._value2member_map_
                        else UserRole.RESIDENT,
                        CredentialType.RESIDENT,
                    )
                    cred_type = str(details.get("credential_type", default_cred_type.value))

                    expires_at: Optional[int] = details.get("expires_at")
                    if action == CredentialAction.ISSUE_VISITOR_PASS and expires_at is None:
                        duration_seconds = int(
                            details.get(
                                "duration_seconds",
                                AccessControl.VISITOR_PASS_DEFAULT_DURATION_SECONDS,
                            )
                        )
                        expires_at = payload.timestamp + AccessControl._seconds_to_ticks(
                            duration_seconds
                        )

                    await conn.execute(
                        """
                        INSERT INTO credentials (
                            credential_id,
                            user_id,
                            credential_type,
                            status,
                            suspension_reason,
                            suspension_start,
                            expires_at,
                            allowed_floors,
                            issued_by_admin,
                            created_at,
                            updated_at
                        )
                        VALUES ($1, $2, $3, $4, NULL, NULL, $5, $6::jsonb, $7, $8, $8)
                        """,
                        payload.target_credential_id,
                        target_user_id,
                        cred_type,
                        CredentialStatus.ACTIVE.value,
                        expires_at,
                        json.dumps(allowed_floors),
                        admin_user_id,
                        payload.timestamp,
                    )

                    audit_type = (
                        AuditActionType.VISITOR_PASS_ISSUED
                        if action == CredentialAction.ISSUE_VISITOR_PASS
                        else AuditActionType.CREDENTIAL_ISSUED
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=audit_type,
                        target_id=payload.target_credential_id,
                        event_payload={
                            "user_id": target_user_id,
                            "credential_id": payload.target_credential_id,
                            "role": target_role,
                            "allowed_floors": allowed_floors,
                            "expires_at": expires_at,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.allow(
                        message=f"Credential '{payload.target_credential_id}' issued.",
                        credential_id=payload.target_credential_id,
                        details={
                            "user_id": target_user_id,
                            "credential_id": payload.target_credential_id,
                            "role": target_role,
                            "allowed_floors": allowed_floors,
                            "expires_at": expires_at,
                        },
                    )

                # --- Actions requiring an existing target_credential_id: SUSPEND, REINSTATE, REVOKE ---
                if not payload.target_credential_id:
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message=f"target_credential_id is required for action '{action}'.",
                        credential_id=payload.admin_credential_id,
                    )

                target_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.target_credential_id, payload.timestamp, db_pool=db_pool
                )
                if target_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Target credential '{payload.target_credential_id}' not found.",
                        credential_id=payload.admin_credential_id,
                    )

                target_status = target_record["status"]
                target_role = target_record["user_role"]
                target_is_admin = (
                    UserRole.is_admin(target_role)
                    or target_record["credential_type"] == CredentialType.ADMIN
                )

                # Peer-administration enforcement for Admin credentials (SEC-2.17)
                if target_is_admin and (
                    payload.admin_credential_id == payload.target_credential_id
                    or admin_user_id == target_record["user_id"]
                ):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=payload.target_credential_id,
                        event_payload={
                            "attempted_operation": str(action),
                            "reason": DenialReason.PEER_ADMIN_REQUIRED.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.PEER_ADMIN_REQUIRED,
                        message="Peer administration required: an Admin cannot self-suspend, self-reinstate, or self-revoke their own Admin card.",
                        credential_id=payload.admin_credential_id,
                    )

                if action == CredentialAction.SUSPEND:
                    if target_status != CredentialStatus.ACTIVE:
                        return AuthorizationResult.deny(
                            DenialReason.INVALID_STATE_TRANSITION,
                            message=f"Cannot transition credential from '{target_status}' to 'SUSPENDED'.",
                            credential_id=payload.admin_credential_id,
                        )
                    raw_reason = details.get("suspension_reason", SuspensionReason.LOST.value)
                    suspension_reason = (
                        str(raw_reason)
                        if raw_reason in SuspensionReason._value2member_map_
                        else SuspensionReason.ADMIN_MANUAL.value
                    )

                    await conn.execute(
                        """
                        UPDATE credentials
                        SET status = $2,
                            suspension_reason = $3,
                            suspension_start = $4,
                            updated_at = $4
                        WHERE credential_id = $1
                        """,
                        payload.target_credential_id,
                        CredentialStatus.SUSPENDED.value,
                        suspension_reason,
                        payload.timestamp,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.CREDENTIAL_SUSPENDED,
                        target_id=payload.target_credential_id,
                        event_payload={
                            "credential_id": payload.target_credential_id,
                            "reason": suspension_reason,
                            "window_hours": (
                                72 if suspension_reason == SuspensionReason.LOST else None
                            ),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.allow(
                        message=f"Credential '{payload.target_credential_id}' suspended ({suspension_reason}).",
                        credential_id=payload.target_credential_id,
                    )

                if action == CredentialAction.REINSTATE:
                    if target_status != CredentialStatus.SUSPENDED:
                        return AuthorizationResult.deny(
                            DenialReason.INVALID_STATE_TRANSITION,
                            message=f"Cannot reinstate credential with status '{target_status}' (must be 'SUSPENDED').",
                            credential_id=payload.admin_credential_id,
                        )
                    await conn.execute(
                        """
                        UPDATE credentials
                        SET status = $2,
                            suspension_reason = NULL,
                            suspension_start = NULL,
                            updated_at = $3
                        WHERE credential_id = $1
                        """,
                        payload.target_credential_id,
                        CredentialStatus.ACTIVE.value,
                        payload.timestamp,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.CREDENTIAL_REINSTATED,
                        target_id=payload.target_credential_id,
                        event_payload={
                            "credential_id": payload.target_credential_id,
                            "notes": details.get("notes", "Identity verified by Admin."),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.allow(
                        message=f"Credential '{payload.target_credential_id}' reinstated to ACTIVE.",
                        credential_id=payload.target_credential_id,
                    )

                if action == CredentialAction.REVOKE:
                    if target_status not in (
                        CredentialStatus.ACTIVE,
                        CredentialStatus.SUSPENDED,
                    ):
                        return AuthorizationResult.deny(
                            DenialReason.INVALID_STATE_TRANSITION,
                            message=f"Cannot revoke credential from status '{target_status}'.",
                            credential_id=payload.admin_credential_id,
                        )
                    revocation_reason = details.get("revocation_reason", "ADMIN_ACTION")
                    await conn.execute(
                        """
                        UPDATE credentials
                        SET status = $2,
                            revocation_reason = $3,
                            revoked_at = $4,
                            updated_at = $4
                        WHERE credential_id = $1
                        """,
                        payload.target_credential_id,
                        CredentialStatus.REVOKED.value,
                        revocation_reason,
                        payload.timestamp,
                    )
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.CREDENTIAL_REVOKED,
                        target_id=payload.target_credential_id,
                        event_payload={
                            "credential_id": payload.target_credential_id,
                            "trigger": revocation_reason,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.allow(
                        message=f"Credential '{payload.target_credential_id}' permanently revoked.",
                        credential_id=payload.target_credential_id,
                    )

                return AuthorizationResult.deny(
                    DenialReason.MALFORMED_MSG_REJECTED,
                    message=f"Unsupported credential management action '{action}'.",
                    credential_id=payload.admin_credential_id,
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.admin_credential_id,
            )

    # -------------------------------------------------------------------------
    # 4.9 RevokeAndReissuePayload Validation (`SEC-2.17, VT-17`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def replaceLostCard(
        payload: RevokeAndReissuePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """
        Atomic "Revoke & Re-issue" workflow (`SEC-2.17, VT-17`).
        Revokes `lost_credential_id` and issues `replacement_credential_id` with identical
        user association, role, and floor permissions in a single ACID transaction.
        """
        try:
            async with db_pool.transaction() as conn:
                # Step 1: Admin Authorization & Peer Check (SEC-2.17)
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.admin_credential_id, payload.timestamp, db_pool=db_pool
                )
                if admin_record is None or admin_record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if admin_record is None
                        else DenialReason(f"CARD_{admin_record['status']}")
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="Active Admin credential required to replace a lost card.",
                        credential_id=payload.admin_credential_id,
                    )

                admin_role = admin_record["user_role"]
                admin_user_id = admin_record["user_id"]
                if not UserRole.is_admin(admin_role):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=payload.lost_credential_id,
                        event_payload={
                            "attempted_operation": AuditActionType.CREDENTIAL_REPLACED.value,
                            "missing_required_role": [UserRole.ADMIN.value],
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.UNAUTHORIZED_CMD_REJECTED,
                        message=f"Role '{admin_role}' is not authorized to replace lost credentials.",
                        credential_id=payload.admin_credential_id,
                    )

                lost_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.lost_credential_id, payload.timestamp, db_pool=db_pool
                )
                if lost_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Lost credential '{payload.lost_credential_id}' does not exist.",
                        credential_id=payload.admin_credential_id,
                    )

                lost_is_admin = (
                    UserRole.is_admin(lost_record["user_role"])
                    or lost_record["credential_type"] == CredentialType.ADMIN
                )
                if lost_is_admin and (
                    payload.admin_credential_id == payload.lost_credential_id
                    or admin_user_id == lost_record["user_id"]
                ):
                    await Logging.write_audit(
                        timestamp=payload.timestamp,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id=payload.lost_credential_id,
                        event_payload={
                            "attempted_operation": AuditActionType.CREDENTIAL_REPLACED.value,
                            "reason": DenialReason.PEER_ADMIN_REQUIRED.value,
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.PEER_ADMIN_REQUIRED,
                        message="An Admin cannot self-reissue a lost Admin card; peer Admin authorization is required.",
                        credential_id=payload.admin_credential_id,
                    )

                # Step 2: Replacement Availability Check
                existing_replacement = await conn.fetchrow(
                    "SELECT credential_id FROM credentials WHERE credential_id = $1",
                    payload.replacement_credential_id,
                )
                if existing_replacement is not None:
                    return AuthorizationResult.deny(
                        DenialReason.REPLACEMENT_ID_IN_USE,
                        message=f"Replacement credential '{payload.replacement_credential_id}' is already registered.",
                        credential_id=payload.admin_credential_id,
                    )

                # Step 3: Atomic ACID Transaction Execution
                allowed_floors = AccessControl._parse_allowed_floors(
                    lost_record["allowed_floors"],
                    role=lost_record["user_role"],
                    assigned_floor=lost_record.get("assigned_floor"),
                )

                await conn.execute(
                    """
                    UPDATE credentials
                    SET status = $2,
                        revocation_reason = $3,
                        revoked_at = $4,
                        updated_at = $4
                    WHERE credential_id = $1
                    """,
                    payload.lost_credential_id,
                    CredentialStatus.REVOKED.value,
                    payload.reason,
                    payload.timestamp,
                )

                await conn.execute(
                    """
                    INSERT INTO credentials (
                        credential_id,
                        user_id,
                        credential_type,
                        status,
                        suspension_reason,
                        suspension_start,
                        revocation_reason,
                        revoked_at,
                        expires_at,
                        allowed_floors,
                        issued_by_admin,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, NULL, NULL, NULL, NULL, $5, $6::jsonb, $7, $8, $8)
                    """,
                    payload.replacement_credential_id,
                    lost_record["user_id"],
                    lost_record["credential_type"],
                    CredentialStatus.ACTIVE.value,
                    lost_record["expires_at"],
                    json.dumps(allowed_floors),
                    admin_user_id,
                    payload.timestamp,
                )

                # Step 4: Audit Logging & Non-Repudiation
                await Logging.write_audit(
                    timestamp=payload.timestamp,
                    actor_id=payload.admin_credential_id,
                    actor_role=admin_role,
                    action_type=AuditActionType.CREDENTIAL_REPLACED,
                    target_id=payload.replacement_credential_id,
                    event_payload={
                        "lost_credential_id": payload.lost_credential_id,
                        "replacement_credential_id": payload.replacement_credential_id,
                        "user_id": lost_record["user_id"],
                        "role": lost_record["user_role"],
                        "assigned_floors": allowed_floors,
                    },
                    db_pool=db_pool,
                )

                return AuthorizationResult.allow(
                    message=(
                        f"Lost credential '{payload.lost_credential_id}' revoked and replaced with "
                        f"'{payload.replacement_credential_id}'."
                    ),
                    credential_id=payload.replacement_credential_id,
                    details={
                        "lost_credential_id": payload.lost_credential_id,
                        "replacement_credential_id": payload.replacement_credential_id,
                        "user_id": lost_record["user_id"],
                        "role": lost_record["user_role"],
                        "assigned_floors": allowed_floors,
                    },
                )
        except Exception:
            return AuthorizationResult.deny(
                DenialReason.DB_FAIL_CLOSED,
                message="Database error: fail-closed policy enforced.",
                credential_id=payload.admin_credential_id,
            )

    # Snake_case alias for consistency with other static methods
    replace_lost_card = replaceLostCard

    # CamelCase aliases matching Section 4
    validateCardSwipe = validate_card_swipe
    validateCarDestination = validate_car_destination
    validateE5ServiceRun = validate_e5_service_run
    validateMaintenanceOperation = validate_maintenance_operation
    validateReturnToService = validate_return_to_service
    validateEmergencyOverride = validate_emergency_override
    validateRescueCarDispatch = validate_rescue_car_dispatch
    manageCredential = manage_credential

    @staticmethod
    async def run_audit_integrity_check(
        admin_credential_id: str,
        timestamp: int = 0,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> tuple[AuthorizationResult, Optional[IntegrityCheckResult]]:
        """
        Authorized wrapper for `ANA-5.8`: verifies that `admin_credential_id` belongs to
        `ADMIN`, then executes `Logging.verify_audit_chain`.
        """
        try:
            async with db_pool.acquire() as conn:
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, admin_credential_id, timestamp, db_pool=db_pool
                )
                if admin_record is None or admin_record["status"] != CredentialStatus.ACTIVE:
                    reason = (
                        DenialReason.UNREGISTERED_CARD
                        if admin_record is None
                        else DenialReason(f"CARD_{admin_record['status']}")
                    )
                    return (
                        AuthorizationResult.deny(
                            reason,
                            message="Active Admin credential required for audit integrity check.",
                            credential_id=admin_credential_id,
                        ),
                        None,
                    )

                role = admin_record["user_role"]
                if not UserRole.is_admin(role):
                    await Logging.write_audit(
                        timestamp=timestamp,
                        actor_id=admin_credential_id,
                        actor_role=role,
                        action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                        target_id="audit_log_chain",
                        event_payload={
                            "attempted_operation": AuditActionType.INTEGRITY_CHECK_RUN.value,
                            "missing_required_role": [UserRole.ADMIN.value],
                        },
                        db_pool=db_pool,
                    )
                    return (
                        AuthorizationResult.deny(
                            DenialReason.UNAUTHORIZED_CMD_REJECTED,
                            message=f"Role '{role}' cannot run audit integrity check.",
                            credential_id=admin_credential_id,
                        ),
                        None,
                    )

            check_result = await Logging.verify_audit_chain(
                admin_credential_id=admin_credential_id,
                admin_role=role,
                timestamp=timestamp,
                db_pool=db_pool,
            )
            return (
                AuthorizationResult.allow(
                    message="Audit chain integrity check completed.",
                    credential_id=admin_credential_id,
                    details={
                        "passed": check_result.passed,
                        "total_records_inspected": check_result.total_records_inspected,
                        "mismatch_index": check_result.mismatch_index,
                        "reason": check_result.reason,
                    },
                ),
                check_result,
            )
        except Exception:
            return (
                AuthorizationResult.deny(
                    DenialReason.DB_FAIL_CLOSED,
                    message="Database error: fail-closed policy enforced.",
                    credential_id=admin_credential_id,
                ),
                None,
            )
