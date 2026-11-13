from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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


class DashboardOperationType(StrEnum):
    VIEW_FLEET_STATUS = "VIEW_FLEET_STATUS"
    VIEW_DIAGNOSTICS = "VIEW_DIAGNOSTICS"
    MAINTENANCE_CONTROL = "MAINTENANCE_CONTROL"
    MANAGE_CREDENTIALS = "MANAGE_CREDENTIALS"
    EMERGENCY_OVERRIDE = "EMERGENCY_OVERRIDE"
    VIEW_AUDIT_LOGS = "VIEW_AUDIT_LOGS"
    RUN_AUDIT_INTEGRITY = "RUN_AUDIT_INTEGRITY"
    EXPORT_ANALYTICS_REPORTS = "EXPORT_ANALYTICS_REPORTS"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


DASHBOARD_PERMISSIONS: dict[str, dict[DashboardOperationType, bool]] = {
    UserRole.ADMIN: {op: True for op in DashboardOperationType},
    UserRole.TECHNICIAN: {
        DashboardOperationType.VIEW_FLEET_STATUS: True,
        DashboardOperationType.VIEW_DIAGNOSTICS: True,
        DashboardOperationType.MAINTENANCE_CONTROL: True,
        DashboardOperationType.MANAGE_CREDENTIALS: False,
        DashboardOperationType.EMERGENCY_OVERRIDE: False,
        DashboardOperationType.VIEW_AUDIT_LOGS: False,
        DashboardOperationType.RUN_AUDIT_INTEGRITY: False,
        DashboardOperationType.EXPORT_ANALYTICS_REPORTS: False,
    },
    UserRole.RESIDENT: {op: False for op in DashboardOperationType},
    UserRole.VISITOR: {op: False for op in DashboardOperationType},
    UserRole.BUILDING_SERVICE: {op: False for op in DashboardOperationType},
}


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
    """4.1 & 7.2 Credential Presentation on In-Cabin Reader (`UI-8`) or Dashboard (`UI-3, UI-10`)."""

    elevator_id: Optional[ElevatorId | str] = None
    credential_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    origin_floor: int = 1
    is_dashboard_access: bool = False


@dataclass
class DashboardOperationPayload:
    """7.5 Privileged Dashboard Operation Validation Contract (`SEC-2.13, SEC-2.15, OR-19`)."""

    session_token: str
    op_type: DashboardOperationType | str
    timestamp: datetime = field(default_factory=datetime.now)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CarDestinationRequestPayload:
    """4.2 Passenger Floor Button Press inside an Elevator Cab."""

    elevator_id: ElevatorId | str
    origin_floor: int
    requested_floor: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class E5ServiceRunPayload:
    """4.3 Dedicated Freight Run (`FR-1.13`)."""

    elevator_id: ElevatorId | str
    destination_floor: int
    purpose: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MaintenanceOperationPayload:
    """4.4 Technician Isolation & Diagnostics (`MNT-4.3`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    operation_type: MaintenanceOperationType | str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ReturnToServicePayload:
    """4.5 Restoring Elevator to Service (`MNT-4.6`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    technician_confirmation: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class EmergencyOverridePayload:
    """4.6 Admin Safety Overrides (`EMR-3.8`)."""

    credential_id: str
    override_type: EmergencyOverrideType | str
    target_car_id: Optional[ElevatorId | str]
    incident_id: Optional[str]
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RescueCarDispatchPayload:
    """4.7 Rescue Car Dispatch (`EMR-3.12, EMR-3.16`)."""

    credential_id: str
    incident_id: str
    rescue_car_id: ElevatorId | str
    incident_floor: int
    dwell_wait_seconds: int = 120
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class CredentialManagementPayload:
    """4.8 Admin Credential Lifecycle Operations (`SEC-2.16`)."""

    admin_credential_id: str
    action: CredentialAction | str
    target_user_id: Optional[str] = None
    target_credential_id: Optional[str] = None
    payload_details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RevokeAndReissuePayload:
    """4.9 Atomic Lost Card Replacement (`SEC-2.17, VT-17`)."""

    admin_credential_id: str
    lost_credential_id: str
    replacement_credential_id: str
    reason: str = "LOST_AND_REPLACED"
    timestamp: datetime = field(default_factory=datetime.now)


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

    # Policy time windows (in seconds)
    RESIDENT_VISITOR_WINDOW_SECONDS: int = 5
    STAFF_TECH_WINDOW_SECONDS: int = 30
    CLONE_DETECTION_WINDOW_SECONDS: int = 10
    REPEATED_FAILURES_WINDOW_SECONDS: int = 60
    REPEATED_FAILURES_THRESHOLD: int = 3
    SCANNING_WINDOW_SECONDS: int = 300  # 5 minutes
    SCANNING_UNIQUE_CARDS_THRESHOLD: int = 11
    LOST_CARD_REVIEW_WINDOW_SECONDS: int = 72 * 3600  # 72 hours
    VISITOR_PASS_DEFAULT_DURATION_SECONDS: int = 4 * 3600  # 4 hours
    DASHBOARD_SESSION_TIMEOUT_SECONDS: int = 15 * 60  # 15 minutes (`OR-19, SEC-2.15`)

    # Section 6 & 7: Operational In-Memory State
    active_car_sessions: dict[str, dict[str, Any]] = {}
    active_dashboard_sessions: dict[str, dict[str, Any]] = {}
    recent_presentations_window: list[dict[str, Any]] = []
    failed_attempts_counter: dict[str, list[datetime]] = {}
    unregistered_scan_counter: list[tuple[str, datetime]] = []

    @staticmethod
    def reset_in_memory_state() -> None:
        """Clear all in-memory session tokens and anomaly detection windows."""
        AccessControl.active_car_sessions.clear()
        AccessControl.active_dashboard_sessions.clear()
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

        floor_set = set(floors) | set(COMMON_FLOORS)
        if role == UserRole.RESIDENT and assigned_floor is not None:
            floor_set.add(int(assigned_floor))

        return sorted(floor_set)

    @staticmethod
    def _fail_closed(credential_id: Optional[str] = None) -> AuthorizationResult:
        return AuthorizationResult.deny(
            DenialReason.DB_FAIL_CLOSED,
            message="Database error: fail-closed policy enforced.",
            credential_id=credential_id,
        )

    @staticmethod
    def _check_active_credential(
        record: Optional[dict[str, Any]],
        credential_id: str,
        message: str = "Active credential required.",
    ) -> Optional[AuthorizationResult]:
        if record is None or record["status"] != CredentialStatus.ACTIVE:
            reason = (
                DenialReason.UNREGISTERED_CARD
                if record is None
                else DenialReason(f"CARD_{record['status']}")
            )
            return AuthorizationResult.deny(reason, message=message, credential_id=credential_id)
        return None

    @staticmethod
    async def _reject_unauthorized_role(
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

    @staticmethod
    async def _reject_malformed_msg(
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
            return AccessControl._fail_closed(credential_id)
        return AuthorizationResult.deny(
            DenialReason.MALFORMED_MSG_REJECTED,
            message=message,
            credential_id=credential_id,
        )

    @staticmethod
    async def _check_peer_admin_restriction(
        *,
        admin_credential_id: str,
        admin_user_id: str,
        admin_role: str,
        target_record: dict[str, Any],
        operation: str,
        timestamp: datetime,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> Optional[AuthorizationResult]:
        target_is_admin = (
            UserRole.is_admin(target_record["user_role"])
            or target_record["credential_type"] == CredentialType.ADMIN
        )
        if target_is_admin and (
            admin_credential_id == target_record["credential_id"]
            or admin_user_id == target_record["user_id"]
        ):
            await Logging.write_audit(
                timestamp=timestamp,
                actor_id=admin_credential_id,
                actor_role=admin_role,
                action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                target_id=target_record["credential_id"],
                event_payload={
                    "attempted_operation": operation,
                    "reason": DenialReason.PEER_ADMIN_REQUIRED.value,
                },
                db_pool=db_pool,
            )
            return AuthorizationResult.deny(
                DenialReason.PEER_ADMIN_REQUIRED,
                message="Peer administration required: an Admin cannot self-manage or self-reissue their own Admin card.",
                credential_id=admin_credential_id,
            )
        return None

    @staticmethod
    async def _record_failed_attempt(
        *,
        credential_id: str,
        car_id: Optional[str],
        timestamp: datetime,
        reason: str,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        cutoff = timestamp - timedelta(seconds=AccessControl.REPEATED_FAILURES_WINDOW_SECONDS)
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
        timestamp: datetime,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        cutoff = timestamp - timedelta(seconds=AccessControl.SCANNING_WINDOW_SECONDS)
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
    async def _apply_credential_transitions(
        target: dict[str, Any] | list[dict[str, Any]] | list[Any],
        current_time: datetime,
        conn: asyncpg.Connection,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
        *,
        is_bulk: Optional[bool] = None,
    ) -> Any:
        """
        Unified state transition engine for both single-card and bulk operations.
        - 4h visitor pass expiration (SEC-2.22) -> transitions ACTIVE to EXPIRED
        - 72h lost-card review window (SEC-2.17) -> transitions SUSPENDED to REVOKED
        When is_bulk is True (or inferred from a list target):
            Returns dict: {"expired_credentials": [...], "revoked_credentials": [...]}
        When is_bulk is False (or inferred from a dict target):
            Mutates record in-place and returns the updated record dict.
        """
        items: list[dict[str, Any]] = [
            dict(r) if not isinstance(r, dict) else r
            for r in (target if isinstance(target, list) else [target])
        ]
        bulk_mode = is_bulk if is_bulk is not None else isinstance(target, list)
        expired_ids: list[str] = []
        revoked_ids: list[str] = []
        review_window = timedelta(seconds=AccessControl.LOST_CARD_REVIEW_WINDOW_SECONDS)

        for rec in items:
            cid = rec["credential_id"]
            status = rec["status"]

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
                    expired_ids.append(cid)
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
                    revoked_ids.append(cid)
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

        if bulk_mode:
            return {
                "expired_credentials": expired_ids,
                "revoked_credentials": revoked_ids,
            }
        return items[0]

    @staticmethod
    async def _fetch_and_refresh_credential(
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
        res = await AccessControl._apply_credential_transitions(
            dict(row), current_time, conn, db_pool, is_bulk=False
        )
        return res

    @staticmethod
    async def refresh_all_credentials(
        current_time: Optional[datetime] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> dict[str, list[str]]:
        ts = Logging.normalize_timestamp(current_time or datetime.now())
        async with db_pool.transaction() as conn:
            rows = await conn.fetch("SELECT * FROM credentials")
            res: dict[str, list[str]] = await AccessControl._apply_credential_transitions(
                rows, ts, conn, db_pool, is_bulk=True
            )
            return res

    # -------------------------------------------------------------------------
    # 4.1 CardSwipePayload Validation (`SEC-2.16, SEC-2.17, SEC-2.18, SEC-2.19, SEC-2.21`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def _handle_if_cloned_swipe(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        clone_window = timedelta(seconds=AccessControl.CLONE_DETECTION_WINDOW_SECONDS)
        AccessControl.recent_presentations_window = [
            e
            for e in AccessControl.recent_presentations_window
            if (ts - e["timestamp"]) < clone_window
        ]
        conflicting = next(
            (
                e
                for e in AccessControl.recent_presentations_window
                if e["credential_id"] == payload.credential_id
                and e["car_id"] != payload.elevator_id
                and timedelta(0) <= (ts - e["timestamp"]) < clone_window
            ),
            None,
        )
        AccessControl.recent_presentations_window.append(
            {
                "credential_id": payload.credential_id,
                "car_id": str(payload.elevator_id),
                "timestamp": ts,
            }
        )
        if conflicting is None:
            return False

        prior_car = conflicting["car_id"]
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
                SET status = $2, suspension_reason = $3, suspension_start = $4, updated_at = $4
                WHERE credential_id = $1
                """,
                payload.credential_id,
                CredentialStatus.SUSPENDED.value,
                SuspensionReason.CLONE_SUSPECTED.value,
                ts,
            )
            await Logging.write_audit(
                timestamp=ts,
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
            created_at=ts,
            db_pool=db_pool,
        )
        await Logging.log_authorization_event(
            timestamp=ts,
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
            timestamp=ts,
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
        return True

    @staticmethod
    async def _handle_unregistered_swipe(
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        target_label = "DASHBOARD" if payload.is_dashboard_access else str(payload.elevator_id)
        repeat_alert = await AccessControl._record_failed_attempt(
            credential_id=payload.credential_id,
            car_id=target_label,
            timestamp=ts,
            reason=DenialReason.UNREGISTERED_CARD,
            db_pool=db_pool,
        )
        scan_alert = await AccessControl._record_unregistered_scan(
            credential_id=payload.credential_id,
            car_id=target_label,
            timestamp=ts,
            db_pool=db_pool,
        )
        alert_fired = repeat_alert or scan_alert

        if not payload.is_dashboard_access:
            await Logging.log_authorization_event(
                timestamp=ts,
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
            timestamp=ts,
            actor_id=payload.credential_id,
            actor_role=UserRole.SYSTEM,
            action_type=AuditActionType.ACCESS_DENIAL,
            target_id=target_label,
            event_payload={
                "elevator_id": payload.elevator_id,
                "is_dashboard_access": payload.is_dashboard_access,
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

    @staticmethod
    async def _handle_inactive_swipe(
        record: dict[str, Any],
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        status = record["status"]
        user_role = record["user_role"]
        alert_fired = False
        car_id_or_none = None if payload.is_dashboard_access else str(payload.elevator_id)
        target_label = "DASHBOARD" if payload.is_dashboard_access else str(payload.elevator_id)

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
                primary_car_id=car_id_or_none,
                details=(
                    f"Suspended credential '{payload.credential_id}' "
                    f"(reason: {record.get('suspension_reason')}) tapped at {target_label}."
                ),
                created_at=ts,
                db_pool=db_pool,
            )
            alert_fired = True
        elif status == CredentialStatus.REVOKED:
            denial_reason = DenialReason.CARD_REVOKED
            await Logging.create_security_alert(
                severity=AlertSeverity.HIGH,
                alert_type=AlertType.REVOKED_CARD_USAGE,
                credential_id=payload.credential_id,
                primary_car_id=car_id_or_none,
                details=f"Revoked credential '{payload.credential_id}' tapped at {target_label}.",
                created_at=ts,
                db_pool=db_pool,
            )
            alert_fired = True
        else:
            denial_reason = DenialReason.CARD_EXPIRED

        repeat_alert = await AccessControl._record_failed_attempt(
            credential_id=payload.credential_id,
            car_id=target_label,
            timestamp=ts,
            reason=denial_reason,
            db_pool=db_pool,
        )
        alert_fired = alert_fired or repeat_alert

        if not payload.is_dashboard_access:
            await Logging.log_authorization_event(
                timestamp=ts,
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
            timestamp=ts,
            actor_id=payload.credential_id,
            actor_role=user_role,
            action_type=AuditActionType.ACCESS_DENIAL,
            target_id=target_label,
            event_payload={
                "elevator_id": payload.elevator_id,
                "is_dashboard_access": payload.is_dashboard_access,
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

    @staticmethod
    async def _verify_swipe_credential(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> tuple[Optional[dict[str, Any]], Optional[AuthorizationResult]]:
        """Fetch, refresh, and verify active credential status for card swipes."""
        record = await AccessControl._fetch_and_refresh_credential(
            conn, payload.credential_id, ts, db_pool
        )
        if record is None:
            return None, await AccessControl._handle_unregistered_swipe(payload, ts, db_pool)

        if record["status"] in (
            CredentialStatus.SUSPENDED,
            CredentialStatus.REVOKED,
            CredentialStatus.EXPIRED,
        ):
            return None, await AccessControl._handle_inactive_swipe(record, payload, ts, db_pool)

        return record, None

    @staticmethod
    async def _validate_dashboard_swipe(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        """Handle dashboard card-based login and 15-minute session creation (§7.2, §7.3)."""
        record, denial = await AccessControl._verify_swipe_credential(
            conn, payload, ts, db_pool
        )
        if denial is not None:
            return denial
        assert record is not None

        user_role = record["user_role"]
        if user_role not in (UserRole.ADMIN, UserRole.TECHNICIAN):
            await Logging.write_audit(
                timestamp=ts,
                actor_id=payload.credential_id,
                actor_role=user_role,
                action_type=AuditActionType.ACCESS_DENIAL,
                target_id="DASHBOARD",
                event_payload={
                    "denial_reason": DenialReason.DASHBOARD_ACCESS_DENIED.value,
                    "role": user_role,
                    "is_dashboard_access": True,
                },
                db_pool=db_pool,
            )
            return AuthorizationResult.deny(
                DenialReason.DASHBOARD_ACCESS_DENIED,
                message=f"Role '{user_role}' is not authorized for dashboard access.",
                credential_id=payload.credential_id,
            )

        session_token = str(uuid.uuid4())
        expires_at = ts + timedelta(seconds=AccessControl.DASHBOARD_SESSION_TIMEOUT_SECONDS)
        AccessControl.active_dashboard_sessions[session_token] = {
            "session_token": session_token,
            "credential_id": payload.credential_id,
            "user_id": record["user_id"],
            "role": user_role,
            "created_at": ts,
            "last_active_at": ts,
            "expires_at": expires_at,
        }
        await Logging.write_audit(
            timestamp=ts,
            actor_id=payload.credential_id,
            actor_role=user_role,
            action_type=AuditActionType.ADMIN_LOGIN,
            target_id="DASHBOARD",
            event_payload={
                "session_token": session_token,
                "credential_id": payload.credential_id,
                "role": user_role,
            },
            db_pool=db_pool,
        )
        return AuthorizationResult.allow(
            message="Dashboard session established.",
            session_token=session_token,
            credential_id=payload.credential_id,
            details={
                "user_id": record["user_id"],
                "role": user_role,
                "expires_at": expires_at,
            },
        )

    @staticmethod
    async def _validate_cabin_swipe(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        """Handle in-cabin card swipe, clone detection, and per-car token issuance (§4.1)."""
        # Step 1: Cloned Card Check (< 10s across different cars, SEC-2.18)
        if await AccessControl._handle_if_cloned_swipe(conn, payload, ts, db_pool):
            return AuthorizationResult.deny(
                DenialReason.CARD_SUSPENDED,
                message=f"Cloned credential detected on {payload.elevator_id}; card suspended.",
                credential_id=payload.credential_id,
                alert_triggered=True,
                details={
                    "suspension_reason": SuspensionReason.CLONE_SUSPECTED.value,
                },
            )

        # Step 2: Credential Registration & Status Lookup (SEC-2.16, SEC-2.19)
        record, denial = await AccessControl._verify_swipe_credential(
            conn, payload, ts, db_pool
        )
        if denial is not None:
            return denial
        assert record is not None

        # Step 3 & 4: Role & Floor Extraction + Per-Car Session Token (SEC-2.21)
        user_role = record["user_role"]
        allowed_floors = AccessControl._parse_allowed_floors(
            record["allowed_floors"],
            role=user_role,
            assigned_floor=record.get("assigned_floor"),
        )
        timeout_sec = (
            AccessControl.STAFF_TECH_WINDOW_SECONDS
            if user_role in (UserRole.BUILDING_SERVICE, UserRole.TECHNICIAN)
            else AccessControl.RESIDENT_VISITOR_WINDOW_SECONDS
        )
        token = str(uuid.uuid4())
        expires_at = ts + timedelta(seconds=timeout_sec)
        AccessControl.active_car_sessions[str(payload.elevator_id)] = {
            "session_token": token,
            "credential_id": payload.credential_id,
            "user_id": record["user_id"],
            "role": user_role,
            "allowed_floors": allowed_floors,
            "granted_at": ts,
            "expires_at": expires_at,
            "consumed": False,
        }
        return AuthorizationResult.allow(
            message="Card verified; destination selection window active.",
            session_token=token,
            credential_id=payload.credential_id,
            details={
                "elevator_id": str(payload.elevator_id),
                "role": user_role,
                "allowed_floors": allowed_floors,
                "expires_at": expires_at,
            },
        )

    @staticmethod
    async def validate_card_swipe(
        payload: CardSwipePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)

        # Case: Malformed payload
        if not payload.is_dashboard_access and not ElevatorId.has_value(payload.elevator_id):
            return await AccessControl._reject_malformed_msg(
                timestamp=ts,
                target_id=str(payload.elevator_id),
                source="CardSwipePayload",
                payload_details={
                    "elevator_id": str(payload.elevator_id),
                    "credential_id": payload.credential_id,
                    "violation": "INVALID_ELEVATOR_ID",
                },
                message=f"Unknown elevator_id '{payload.elevator_id}'.",
                credential_id=payload.credential_id,
                db_pool=db_pool,
            )

        try:
            async with db_pool.transaction() as conn:
                if payload.is_dashboard_access:
                    return await AccessControl._validate_dashboard_swipe(
                        conn, payload, ts, db_pool
                    )
                return await AccessControl._validate_cabin_swipe(
                    conn, payload, ts, db_pool
                )
        except Exception:
            return AccessControl._fail_closed(payload.credential_id)

    # -------------------------------------------------------------------------
    # 4.2 CarDestinationRequestPayload Validation (`SEC-2.5, SEC-2.6, SEC-2.21`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def _deny_destination_selection(
        *,
        payload: CarDestinationRequestPayload,
        cred_id: Optional[str],
        role: str,
        reason: DenialReason,
        message: str,
        action_type: AuditActionType,
        token_age_seconds: Optional[float] = None,
        record_failure: bool = True,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        alert_fired = False
        if record_failure and cred_id:
            alert_fired = await AccessControl._record_failed_attempt(
                credential_id=cred_id,
                car_id=str(payload.elevator_id),
                timestamp=ts,
                reason=reason,
                db_pool=db_pool,
            )
        await Logging.log_authorization_event(
            timestamp=ts,
            credential_id=cred_id,
            elevator_id=str(payload.elevator_id),
            origin_floor=payload.origin_floor,
            destination_floor=payload.requested_floor,
            outcome=AuthorizationOutcome.DENIED,
            denial_reason=reason,
            alert_triggered=alert_fired,
            db_pool=db_pool,
        )
        event_payload: dict[str, Any] = {
            "elevator_id": str(payload.elevator_id),
            "denial_reason": reason.value,
        }
        if action_type == AuditActionType.ONE_TAP_VIOLATION:
            event_payload["denied_floor"] = payload.requested_floor
            if token_age_seconds is not None:
                event_payload["token_age_seconds"] = token_age_seconds
        else:
            event_payload["origin_floor"] = payload.origin_floor
            event_payload["requested_floor"] = payload.requested_floor

        await Logging.write_audit(
            timestamp=ts,
            actor_id=cred_id or UserRole.SYSTEM,
            actor_role=role,
            action_type=action_type,
            target_id=str(payload.elevator_id),
            event_payload=event_payload,
            db_pool=db_pool,
        )
        return AuthorizationResult.deny(
            reason,
            message=message,
            credential_id=cred_id,
            alert_triggered=alert_fired,
        )

    @staticmethod
    async def validate_car_destination(
        payload: CarDestinationRequestPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        if not ElevatorId.has_value(payload.elevator_id) or not (
            payload.requested_floor == -1 or (1 <= payload.requested_floor <= 30)
        ):
            return await AccessControl._reject_malformed_msg(
                timestamp=ts,
                target_id=str(payload.elevator_id),
                source="CarDestinationRequestPayload",
                payload_details={
                    "elevator_id": str(payload.elevator_id),
                    "origin_floor": payload.origin_floor,
                    "requested_floor": payload.requested_floor,
                },
                message="Invalid elevator_id or requested_floor.",
                db_pool=db_pool,
            )

        try:
            async with db_pool.transaction():
                # Step 1: B1 Universal Exemption (SEC-2.5) & E5 B1 Prohibition (FR-1.10)
                if payload.requested_floor == -1:
                    if payload.elevator_id == ElevatorId.E5:
                        session = AccessControl.active_car_sessions.get(ElevatorId.E5.value)
                        cred_id = session["credential_id"] if session else None
                        actor_role = session["role"] if session else UserRole.SYSTEM
                        return await AccessControl._deny_destination_selection(
                            payload=payload,
                            cred_id=cred_id,
                            role=actor_role,
                            reason=DenialReason.E5_B1_FORBIDDEN,
                            message="Freight elevator E5 does not serve Basement 1 (B1).",
                            action_type=AuditActionType.ACCESS_DENIAL,
                            record_failure=False,
                            ts=ts,
                            db_pool=db_pool,
                        )

                    await Logging.log_authorization_event(
                        timestamp=ts,
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
                    return await AccessControl._deny_destination_selection(
                        payload=payload,
                        cred_id=None,
                        role=UserRole.SYSTEM,
                        reason=DenialReason.NO_ACTIVE_CREDENTIAL,
                        message="No active credential session in this elevator.",
                        action_type=AuditActionType.ACCESS_DENIAL,
                        record_failure=False,
                        ts=ts,
                        db_pool=db_pool,
                    )

                cred_id, role = session["credential_id"], session["role"]
                if session.get("consumed", False):
                    return await AccessControl._deny_destination_selection(
                        payload=payload,
                        cred_id=cred_id,
                        role=role,
                        reason=DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        message="Single-use tap token already consumed; please re-tap credential.",
                        action_type=AuditActionType.ONE_TAP_VIOLATION,
                        token_age_seconds=(ts - session["granted_at"]).total_seconds(),
                        record_failure=True,
                        ts=ts,
                        db_pool=db_pool,
                    )

                if ts > session["expires_at"]:
                    return await AccessControl._deny_destination_selection(
                        payload=payload,
                        cred_id=cred_id,
                        role=role,
                        reason=DenialReason.TOKEN_WINDOW_EXPIRED,
                        message="Credential selection window expired; please re-tap credential.",
                        action_type=AuditActionType.ACCESS_DENIAL,
                        record_failure=True,
                        ts=ts,
                        db_pool=db_pool,
                    )

                # Step 3: Floor & E5 Role Authorization Check (Section 1.2 & 4.2)
                e5_forbidden = (
                    payload.elevator_id == ElevatorId.E5
                    and not UserRole.can_request_e5_freight(role)
                )
                floor_forbidden = payload.requested_floor not in session["allowed_floors"]

                if e5_forbidden or floor_forbidden:
                    return await AccessControl._deny_destination_selection(
                        payload=payload,
                        cred_id=cred_id,
                        role=role,
                        reason=DenialReason.UNAUTHORIZED_FLOOR,
                        message=f"Credential '{cred_id}' is not authorized for floor {payload.requested_floor} on {payload.elevator_id}.",
                        action_type=AuditActionType.ACCESS_DENIAL,
                        record_failure=True,
                        ts=ts,
                        db_pool=db_pool,
                    )

                # Step 4: Consume Token (SEC-2.21)
                session["consumed"] = True
                await Logging.log_authorization_event(
                    timestamp=ts,
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
            return AccessControl._fail_closed()

    # -------------------------------------------------------------------------
    # 4.3 E5ServiceRunPayload Validation (`FR-1.13`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_e5_service_run(
        payload: E5ServiceRunPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction():
                if payload.elevator_id != ElevatorId.E5 or not (
                    1 <= payload.destination_floor <= 30
                ):
                    reason = (
                        DenialReason.E5_B1_FORBIDDEN
                        if payload.destination_floor == -1
                        else DenialReason.MALFORMED_MSG_REJECTED
                    )
                    await Logging.write_audit(
                        timestamp=ts,
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

                cred_id, role = session["credential_id"], session["role"]
                if session.get("consumed", False):
                    await Logging.write_audit(
                        timestamp=ts,
                        actor_id=cred_id,
                        actor_role=role,
                        action_type=AuditActionType.ONE_TAP_VIOLATION,
                        target_id=ElevatorId.E5,
                        event_payload={
                            "elevator_id": ElevatorId.E5.value,
                            "denied_floor": payload.destination_floor,
                            "token_age_seconds": (ts - session["granted_at"]).total_seconds(),
                        },
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                        message="Session token already consumed.",
                        credential_id=cred_id,
                    )

                if ts > session["expires_at"]:
                    return AuthorizationResult.deny(
                        DenialReason.TOKEN_WINDOW_EXPIRED,
                        message="Session token expired.",
                        credential_id=cred_id,
                    )

                if not UserRole.can_initiate_service_run(role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=cred_id,
                        actor_role=role,
                        target_id=ElevatorId.E5.value,
                        operation=AuditActionType.E5_SERVICE_RUN_START.value,
                        required_roles=[
                            UserRole.BUILDING_SERVICE.value,
                            UserRole.TECHNICIAN.value,
                            UserRole.ADMIN.value,
                        ],
                        message=f"Role '{role}' is not authorized to initiate an E5 service run.",
                        extra_payload={
                            "destination_floor": payload.destination_floor,
                            "purpose": payload.purpose,
                        },
                        db_pool=db_pool,
                    )

                session["consumed"] = True
                await Logging.write_audit(
                    timestamp=ts,
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
            return AccessControl._fail_closed()

    # -------------------------------------------------------------------------
    # 4.4 MaintenanceOperationPayload Validation (`MNT-4.3`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_maintenance_operation(
        payload: MaintenanceOperationPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(
                    payload.elevator_id
                ) or not MaintenanceOperationType.has_value(payload.operation_type):
                    return await AccessControl._reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.elevator_id),
                        source="MaintenanceOperationPayload",
                        payload_details={
                            "elevator_id": str(payload.elevator_id),
                            "operation_type": str(payload.operation_type),
                        },
                        message="Invalid elevator_id or operation_type.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, ts, db_pool
                )
                denial = AccessControl._check_active_credential(
                    record,
                    payload.credential_id,
                    "Active technician or admin credential required.",
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]
                if not UserRole.can_perform_maintenance(role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        target_id=str(payload.elevator_id),
                        operation=str(payload.operation_type),
                        required_roles=[UserRole.TECHNICIAN.value, UserRole.ADMIN.value],
                        message=f"Role '{role}' cannot execute maintenance operation '{payload.operation_type}'.",
                        db_pool=db_pool,
                    )

                await Logging.write_audit(
                    timestamp=ts,
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
            return AccessControl._fail_closed(payload.credential_id)

    # -------------------------------------------------------------------------
    # 4.5 ReturnToServicePayload Validation (`MNT-4.6`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_return_to_service(
        payload: ReturnToServicePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(payload.elevator_id):
                    return await AccessControl._reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.elevator_id),
                        source="ReturnToServicePayload",
                        payload_details={"elevator_id": str(payload.elevator_id)},
                        message="Invalid elevator_id.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, ts, db_pool
                )
                denial = AccessControl._check_active_credential(
                    record,
                    payload.credential_id,
                    "Active technician or admin credential required.",
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]
                if not UserRole.can_perform_maintenance(role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        target_id=str(payload.elevator_id),
                        operation=AuditActionType.RETURN_TO_SERVICE.value,
                        required_roles=[UserRole.TECHNICIAN.value, UserRole.ADMIN.value],
                        message=f"Role '{role}' is not authorized to return car to service.",
                        db_pool=db_pool,
                    )

                if not payload.technician_confirmation:
                    return AuthorizationResult.deny(
                        DenialReason.CONFIRMATION_REQUIRED,
                        message="Technician safety confirmation is required to return elevator to service.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
                    timestamp=ts,
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
            return AccessControl._fail_closed(payload.credential_id)

    # -------------------------------------------------------------------------
    # 4.6 EmergencyOverridePayload Validation (`EMR-3.8`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_emergency_override(
        payload: EmergencyOverridePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not EmergencyOverrideType.has_value(payload.override_type):
                    return await AccessControl._reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.target_car_id or payload.incident_id or "UNKNOWN"),
                        source="EmergencyOverridePayload",
                        payload_details={"override_type": str(payload.override_type)},
                        message=f"Invalid override_type '{payload.override_type}'.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, ts, db_pool
                )
                denial = AccessControl._check_active_credential(
                    record, payload.credential_id, "Active Admin credential required."
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]
                target_entity = str(payload.target_car_id or payload.incident_id or UserRole.SYSTEM)
                if not UserRole.is_admin(role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        target_id=target_entity,
                        operation=str(payload.override_type),
                        required_roles=[UserRole.ADMIN.value],
                        message=f"Role '{role}' cannot execute emergency override '{payload.override_type}'.",
                        db_pool=db_pool,
                    )

                audit_action = (
                    AuditActionType.INCIDENT_RESOLVED
                    if payload.override_type == EmergencyOverrideType.RESOLVE_INCIDENT
                    else AuditActionType.EMERGENCY_OVERRIDE
                )
                await Logging.write_audit(
                    timestamp=ts,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=audit_action,
                    target_id=target_entity,
                    event_payload={
                        "override_type": str(payload.override_type),
                        "target_car_id": (
                            str(payload.target_car_id) if payload.target_car_id else None
                        ),
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
                        "target_car_id": (
                            str(payload.target_car_id) if payload.target_car_id else None
                        ),
                        "incident_id": payload.incident_id,
                    },
                )
        except Exception:
            return AccessControl._fail_closed(payload.credential_id)

    # -------------------------------------------------------------------------
    # 4.7 RescueCarDispatchPayload Validation (`EMR-3.12, EMR-3.16`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def validate_rescue_car_dispatch(
        payload: RescueCarDispatchPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.credential_id, ts, db_pool
                )
                denial = AccessControl._check_active_credential(
                    record, payload.credential_id, "Active Admin credential required."
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]
                if not UserRole.is_admin(role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.credential_id,
                        actor_role=role,
                        target_id=str(payload.rescue_car_id),
                        operation=AuditActionType.RESCUE_CAR_DISPATCHED.value,
                        required_roles=[UserRole.ADMIN.value],
                        message=f"Role '{role}' is not authorized to dispatch a rescue car.",
                        extra_payload={"incident_id": payload.incident_id},
                        db_pool=db_pool,
                    )

                if not ElevatorId.is_residential(payload.rescue_car_id):
                    await Logging.write_audit(
                        timestamp=ts,
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
                    timestamp=ts,
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
            return AccessControl._fail_closed(payload.credential_id)

    # -------------------------------------------------------------------------
    # 4.8 CredentialManagementPayload Validation (`SEC-2.16, SEC-2.17, SEC-2.22`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def _issue_credential(
        conn: asyncpg.Connection,
        payload: CredentialManagementPayload,
        admin_record: dict[str, Any],
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
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

        details = payload.payload_details or {}
        target_user_id = payload.target_user_id or str(uuid.uuid4())
        user_row = await conn.fetchrow(
            "SELECT user_id, role, assigned_floor FROM users WHERE user_id = $1",
            target_user_id,
        )

        is_visitor = payload.action == CredentialAction.ISSUE_VISITOR_PASS
        default_role = UserRole.VISITOR if is_visitor else UserRole.RESIDENT
        target_role = (
            UserRole.VISITOR.value
            if is_visitor
            else str(details.get("role", user_row["role"] if user_row else default_role))
        )
        assigned_floor = details.get(
            "assigned_floor", user_row["assigned_floor"] if user_row else None
        )

        if user_row is None:
            full_name = details.get("full_name", f"{target_role.title()} {target_user_id[:8]}")
            await conn.execute(
                """
                INSERT INTO users (user_id, full_name, role, apartment_unit, assigned_floor, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $6)
                """,
                target_user_id,
                full_name,
                target_role,
                details.get("apartment_unit"),
                assigned_floor,
                ts,
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

        raw_exp = details.get("expires_at")
        expires_at: Optional[datetime] = (
            Logging.normalize_timestamp(raw_exp) if raw_exp is not None else None
        )
        if is_visitor and expires_at is None:
            dur = int(
                details.get(
                    "duration_seconds",
                    AccessControl.VISITOR_PASS_DEFAULT_DURATION_SECONDS,
                )
            )
            expires_at = ts + timedelta(seconds=dur)

        await conn.execute(
            """
            INSERT INTO credentials (
                credential_id, user_id, credential_type, status, suspension_reason,
                suspension_start, expires_at, allowed_floors, issued_by_admin, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, NULL, NULL, $5, $6::jsonb, $7, $8, $8)
            """,
            payload.target_credential_id,
            target_user_id,
            cred_type,
            CredentialStatus.ACTIVE.value,
            expires_at,
            json.dumps(allowed_floors),
            admin_record["user_id"],
            ts,
        )

        audit_type = (
            AuditActionType.VISITOR_PASS_ISSUED
            if is_visitor
            else AuditActionType.CREDENTIAL_ISSUED
        )
        await Logging.write_audit(
            timestamp=ts,
            actor_id=payload.admin_credential_id,
            actor_role=admin_record["user_role"],
            action_type=audit_type,
            target_id=payload.target_credential_id,
            event_payload={
                "user_id": target_user_id,
                "credential_id": payload.target_credential_id,
                "role": target_role,
                "allowed_floors": allowed_floors,
                "expires_at": (
                    Logging.format_timestamp(expires_at) if expires_at is not None else None
                ),
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

    @staticmethod
    async def _modify_credential_status(
        conn: asyncpg.Connection,
        payload: CredentialManagementPayload,
        admin_record: dict[str, Any],
        target_record: dict[str, Any],
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        action = payload.action
        target_id = payload.target_credential_id
        target_status = target_record["status"]
        admin_id = payload.admin_credential_id
        admin_role = admin_record["user_role"]
        details = payload.payload_details or {}

        if action == CredentialAction.SUSPEND:
            if target_status != CredentialStatus.ACTIVE:
                return AuthorizationResult.deny(
                    DenialReason.INVALID_STATE_TRANSITION,
                    message=f"Cannot transition credential from '{target_status}' to 'SUSPENDED'.",
                    credential_id=admin_id,
                )
            reason = (
                str(details.get("suspension_reason", SuspensionReason.LOST.value))
                if details.get("suspension_reason") in SuspensionReason._value2member_map_
                else SuspensionReason.ADMIN_MANUAL.value
            )
            await conn.execute(
                "UPDATE credentials SET status = $2, suspension_reason = $3, suspension_start = $4, updated_at = $4 WHERE credential_id = $1",
                target_id,
                CredentialStatus.SUSPENDED.value,
                reason,
                ts,
            )
            await Logging.write_audit(
                timestamp=ts,
                actor_id=admin_id,
                actor_role=admin_role,
                action_type=AuditActionType.CREDENTIAL_SUSPENDED,
                target_id=target_id,
                event_payload={
                    "credential_id": target_id,
                    "reason": reason,
                    "window_hours": 72 if reason == SuspensionReason.LOST else None,
                },
                db_pool=db_pool,
            )
            return AuthorizationResult.allow(
                message=f"Credential '{target_id}' suspended ({reason}).",
                credential_id=target_id,
            )

        if action == CredentialAction.REINSTATE:
            if target_status != CredentialStatus.SUSPENDED:
                return AuthorizationResult.deny(
                    DenialReason.INVALID_STATE_TRANSITION,
                    message=f"Cannot reinstate credential with status '{target_status}' (must be 'SUSPENDED').",
                    credential_id=admin_id,
                )
            await conn.execute(
                "UPDATE credentials SET status = $2, suspension_reason = NULL, suspension_start = NULL, updated_at = $3 WHERE credential_id = $1",
                target_id,
                CredentialStatus.ACTIVE.value,
                ts,
            )
            await Logging.write_audit(
                timestamp=ts,
                actor_id=admin_id,
                actor_role=admin_role,
                action_type=AuditActionType.CREDENTIAL_REINSTATED,
                target_id=target_id,
                event_payload={
                    "credential_id": target_id,
                    "notes": details.get("notes", "Identity verified by Admin."),
                },
                db_pool=db_pool,
            )
            return AuthorizationResult.allow(
                message=f"Credential '{target_id}' reinstated to ACTIVE.",
                credential_id=target_id,
            )

        if action == CredentialAction.REVOKE:
            if target_status not in (CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED):
                return AuthorizationResult.deny(
                    DenialReason.INVALID_STATE_TRANSITION,
                    message=f"Cannot revoke credential from status '{target_status}'.",
                    credential_id=admin_id,
                )
            reason = str(details.get("revocation_reason", "ADMIN_ACTION"))
            await conn.execute(
                "UPDATE credentials SET status = $2, revocation_reason = $3, revoked_at = $4, updated_at = $4 WHERE credential_id = $1",
                target_id,
                CredentialStatus.REVOKED.value,
                reason,
                ts,
            )
            await Logging.write_audit(
                timestamp=ts,
                actor_id=admin_id,
                actor_role=admin_role,
                action_type=AuditActionType.CREDENTIAL_REVOKED,
                target_id=target_id,
                event_payload={"credential_id": target_id, "trigger": reason},
                db_pool=db_pool,
            )
            return AuthorizationResult.allow(
                message=f"Credential '{target_id}' permanently revoked.",
                credential_id=target_id,
            )

        return AuthorizationResult.deny(
            DenialReason.MALFORMED_MSG_REJECTED,
            message=f"Unsupported credential management action '{action}'.",
            credential_id=admin_id,
        )

    @staticmethod
    async def manage_credential(
        payload: CredentialManagementPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.admin_credential_id, ts, db_pool=db_pool
                )
                denial = AccessControl._check_active_credential(
                    admin_record,
                    payload.admin_credential_id,
                    "Active Admin credential required.",
                )
                if denial:
                    return denial
                assert admin_record is not None
                admin_role, admin_user_id = admin_record["user_role"], admin_record["user_id"]
                if not UserRole.is_admin(admin_role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        target_id=payload.target_credential_id
                        or payload.target_user_id
                        or "UNKNOWN",
                        operation=str(payload.action),
                        required_roles=[UserRole.ADMIN.value],
                        message=f"Role '{admin_role}' is not authorized to manage credentials.",
                        db_pool=db_pool,
                    )

                if payload.action in (CredentialAction.ISSUE, CredentialAction.ISSUE_VISITOR_PASS):
                    return await AccessControl._issue_credential(
                        conn, payload, admin_record, ts, db_pool
                    )

                if not payload.target_credential_id:
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message=f"target_credential_id is required for action '{payload.action}'.",
                        credential_id=payload.admin_credential_id,
                    )

                target_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.target_credential_id, ts, db_pool=db_pool
                )
                if target_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Target credential '{payload.target_credential_id}' not found.",
                        credential_id=payload.admin_credential_id,
                    )

                peer_denial = await AccessControl._check_peer_admin_restriction(
                    admin_credential_id=payload.admin_credential_id,
                    admin_user_id=admin_user_id,
                    admin_role=admin_role,
                    target_record=target_record,
                    operation=str(payload.action),
                    timestamp=ts,
                    db_pool=db_pool,
                )
                if peer_denial:
                    return peer_denial

                return await AccessControl._modify_credential_status(
                    conn, payload, admin_record, target_record, ts, db_pool
                )
        except Exception:
            return AccessControl._fail_closed(payload.admin_credential_id)

    # -------------------------------------------------------------------------
    # 4.9 RevokeAndReissuePayload Validation (`SEC-2.17, VT-17`)
    # -------------------------------------------------------------------------
    @staticmethod
    async def replace_lost_card(
        payload: RevokeAndReissuePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Atomic Revoke & Re-issue workflow (SEC-2.17, VT-17)."""
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.admin_credential_id, ts, db_pool=db_pool
                )
                denial = AccessControl._check_active_credential(
                    admin_record,
                    payload.admin_credential_id,
                    "Active Admin credential required to replace a lost card.",
                )
                if denial:
                    return denial
                assert admin_record is not None
                admin_role, admin_user_id = admin_record["user_role"], admin_record["user_id"]
                if not UserRole.is_admin(admin_role):
                    return await AccessControl._reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=payload.admin_credential_id,
                        actor_role=admin_role,
                        target_id=payload.lost_credential_id,
                        operation=AuditActionType.CREDENTIAL_REPLACED.value,
                        required_roles=[UserRole.ADMIN.value],
                        message=f"Role '{admin_role}' is not authorized to replace lost credentials.",
                        db_pool=db_pool,
                    )

                lost_record = await AccessControl._fetch_and_refresh_credential(
                    conn, payload.lost_credential_id, ts, db_pool=db_pool
                )
                if lost_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Lost credential '{payload.lost_credential_id}' does not exist.",
                        credential_id=payload.admin_credential_id,
                    )

                peer_denial = await AccessControl._check_peer_admin_restriction(
                    admin_credential_id=payload.admin_credential_id,
                    admin_user_id=admin_user_id,
                    admin_role=admin_role,
                    target_record=lost_record,
                    operation=AuditActionType.CREDENTIAL_REPLACED.value,
                    timestamp=ts,
                    db_pool=db_pool,
                )
                if peer_denial:
                    return peer_denial

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

                allowed_floors = AccessControl._parse_allowed_floors(
                    lost_record["allowed_floors"],
                    role=lost_record["user_role"],
                    assigned_floor=lost_record.get("assigned_floor"),
                )

                await conn.execute(
                    """
                    UPDATE credentials
                    SET status = $2, revocation_reason = $3, revoked_at = $4, updated_at = $4
                    WHERE credential_id = $1
                    """,
                    payload.lost_credential_id,
                    CredentialStatus.REVOKED.value,
                    payload.reason,
                    ts,
                )
                await conn.execute(
                    """
                    INSERT INTO credentials (
                        credential_id, user_id, credential_type, status, suspension_reason,
                        suspension_start, revocation_reason, revoked_at, expires_at,
                        allowed_floors, issued_by_admin, created_at, updated_at
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
                    ts,
                )
                await Logging.write_audit(
                    timestamp=ts,
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
            return AccessControl._fail_closed(payload.admin_credential_id)

    @staticmethod
    async def run_audit_integrity_check(
        admin_credential_id: str,
        timestamp: Optional[datetime] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> tuple[AuthorizationResult, Optional[IntegrityCheckResult]]:
        """Authorized wrapper for ANA-5.8."""
        ts = Logging.normalize_timestamp(timestamp or datetime.now())
        try:
            async with db_pool.acquire() as conn:
                admin_record = await AccessControl._fetch_and_refresh_credential(
                    conn, admin_credential_id, ts, db_pool=db_pool
                )
                denial = AccessControl._check_active_credential(
                    admin_record,
                    admin_credential_id,
                    "Active Admin credential required for audit integrity check.",
                )
                if denial:
                    return denial, None
                assert admin_record is not None
                role = admin_record["user_role"]
                if not UserRole.is_admin(role):
                    await Logging.write_audit(
                        timestamp=ts,
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
                timestamp=ts,
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
            return AccessControl._fail_closed(admin_credential_id), None

    # -------------------------------------------------------------------------
    # 4.7 Dashboard Session Lifecycle & Sliding TTL (`SEC-2.13, SEC-2.15, OR-19`)
    # -------------------------------------------------------------------------

    @staticmethod
    async def validate_dashboard_operation(
        payload: DashboardOperationPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """
        Validate an active 15-minute dashboard session with JIT lazy eviction,
        sliding TTL renewal, and role entitlement checks (§7.4 - §7.6).
        """
        ts = Logging.normalize_timestamp(payload.timestamp)

        if not DashboardOperationType.has_value(payload.op_type):
            return await AccessControl._reject_malformed_msg(
                timestamp=ts,
                target_id="DASHBOARD",
                source="DashboardOperationPayload",
                payload_details={"op_type": str(payload.op_type), "violation": "UNKNOWN_DASHBOARD_OP"},
                message=f"Unknown dashboard operation '{payload.op_type}'.",
                db_pool=db_pool,
            )

        # 1. Session Existence Check
        session = AccessControl.active_dashboard_sessions.get(payload.session_token)
        if not session:
            return AuthorizationResult.deny(
                DenialReason.SESSION_INVALID,
                message="No active dashboard session found.",
            )

        # 2. Virtual Clock Inactivity Expiration Check + JIT Lazy Eviction (`OR-19, SEC-2.15`)
        if ts > session["expires_at"]:
            del AccessControl.active_dashboard_sessions[payload.session_token]
            try:
                await Logging.write_audit(
                    timestamp=ts,
                    actor_id=session["credential_id"],
                    actor_role=session["role"],
                    action_type=AuditActionType.ADMIN_LOGOUT,
                    target_id="DASHBOARD",
                    event_payload={
                        "reason": "TIMEOUT",
                        "session_token": payload.session_token,
                    },
                    db_pool=db_pool,
                )
            except Exception:
                pass
            return AuthorizationResult.deny(
                DenialReason.SESSION_EXPIRED,
                message="Dashboard session timed out after 15 minutes of inactivity.",
                credential_id=session["credential_id"],
            )

        # 3. Reset Sliding Inactivity Timeout (Hold & Extend TTL)
        session["expires_at"] = ts + timedelta(seconds=AccessControl.DASHBOARD_SESSION_TIMEOUT_SECONDS)
        session["last_active_at"] = ts

        # 4. Role Entitlement Verification (§7.4 Permission Matrix)
        user_role = session["role"]
        op_enum = DashboardOperationType(payload.op_type)
        if not DASHBOARD_PERMISSIONS.get(user_role, {}).get(op_enum, False):
            try:
                await Logging.write_audit(
                    timestamp=ts,
                    actor_id=session["credential_id"],
                    actor_role=user_role,
                    action_type=AuditActionType.UNAUTHORIZED_CMD_REJECTED,
                    target_id="DASHBOARD",
                    event_payload={
                        "attempted_operation": op_enum.value,
                        "role": user_role,
                        "reason": "MISSING_REQUIRED_ROLE",
                        "session_token": payload.session_token,
                    },
                    db_pool=db_pool,
                )
            except Exception:
                return AccessControl._fail_closed(session["credential_id"])
            return AuthorizationResult.deny(
                DenialReason.UNAUTHORIZED_ROLE,
                message=f"Role '{user_role}' is not authorized to execute '{op_enum.value}'.",
                credential_id=session["credential_id"],
            )

        # 5. Authorization Granted
        return AuthorizationResult.allow(
            message="Authorized.",
            session_token=payload.session_token,
            credential_id=session["credential_id"],
            details={
                "role": user_role,
                "user_id": session["user_id"],
                "op_type": op_enum.value,
                "expires_at": session["expires_at"],
            },
        )

    @staticmethod
    async def logout_dashboard_session(
        session_token: str,
        timestamp: Optional[datetime] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """
        Explicitly terminate an active dashboard session and emit `ADMIN_LOGOUT`
        with `reason = 'MANUAL'` (§7.6).
        """
        ts = Logging.normalize_timestamp(timestamp or datetime.now())
        session = AccessControl.active_dashboard_sessions.pop(session_token, None)
        if session is None:
            return AuthorizationResult.deny(
                DenialReason.SESSION_INVALID,
                message="No active dashboard session found.",
            )

        try:
            await Logging.write_audit(
                timestamp=ts,
                actor_id=session["credential_id"],
                actor_role=session["role"],
                action_type=AuditActionType.ADMIN_LOGOUT,
                target_id="DASHBOARD",
                event_payload={
                    "reason": "MANUAL",
                    "session_token": session_token,
                },
                db_pool=db_pool,
            )
        except Exception:
            return AccessControl._fail_closed(session["credential_id"])

        return AuthorizationResult.allow(
            message="Dashboard session logged out.",
            credential_id=session["credential_id"],
            details={"reason": "MANUAL", "session_token": session_token},
        )
