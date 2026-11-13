from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Optional
import uuid

import asyncpg

from acs.enums import (
    DASHBOARD_PERMISSIONS,
    ROLE_TO_CREDENTIAL_TYPE,
    AuditActionType,
    CredentialAction,
    CredentialStatus,
    CredentialType,
    DashboardOperationType,
    DenialReason,
    ElevatorId,
    EmergencyOverrideType,
    SuspensionReason,
    UserRole,
)
from acs.helpers import (
    apply_credential_transitions,
    authorize_credential_role,
    fail_closed,
    fetch_and_refresh_credential,
    parse_allowed_floors,
    reject_malformed_msg,
    reset_shared_state,
    verify_swipe_credential,
)
from acs.logs import Logging
from acs.payloads import (
    AuthorizationResult,
    CardSwipePayload,
    CredentialManagementPayload,
    DashboardOperationPayload,
    EmergencyOverridePayload,
    IntegrityCheckResult,
    RescueCarDispatchPayload,
    RevokeAndReissuePayload,
)
from db import DbConnectionPool


class DashboardAccessControl:
    """
    ACS.DashboardAccessControl submodule (`acs/dashboard_accesscontrol.py`).
    Validates authentication, session lifecycle, and role/permission authorization
    for the SREMS management dashboard (dashboard card login, sliding-TTL session
    operations, credential lifecycle management, emergency overrides, rescue car
    dispatch, and audit chain integrity checks).
    """

    VISITOR_PASS_DEFAULT_DURATION_SECONDS: int = 4 * 3600  # 4 hours
    DASHBOARD_SESSION_TIMEOUT_SECONDS: int = 15 * 60  # 15 minutes (`OR-19, SEC-2.15`)

    active_dashboard_sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def reset_in_memory_state() -> None:
        """Clear all dashboard session tokens and shared anomaly detection windows."""
        DashboardAccessControl.active_dashboard_sessions.clear()
        reset_shared_state()

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
    async def refresh_all_credentials(
        current_time: Optional[datetime] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> dict[str, list[str]]:
        ts = Logging.normalize_timestamp(current_time or datetime.now())
        async with db_pool.transaction() as conn:
            rows = await conn.fetch("SELECT * FROM credentials")
            res: dict[str, list[str]] = await apply_credential_transitions(
                rows, ts, conn, db_pool, is_bulk=True
            )
            return res

    @staticmethod
    async def _validate_dashboard_swipe(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> AuthorizationResult:
        """Handle dashboard card-based login and 15-minute session creation."""
        record, denial = await verify_swipe_credential(conn, payload, ts, db_pool)
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
        expires_at = ts + timedelta(
            seconds=DashboardAccessControl.DASHBOARD_SESSION_TIMEOUT_SECONDS
        )
        DashboardAccessControl.active_dashboard_sessions[session_token] = {
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
    async def validate_card_swipe(
        payload: CardSwipePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Validate a card swipe on the dashboard login reader."""
        ts = Logging.normalize_timestamp(payload.timestamp)

        if not payload.is_dashboard_access:
            return await reject_malformed_msg(
                timestamp=ts,
                target_id="DASHBOARD",
                source="CardSwipePayload",
                payload_details={
                    "elevator_id": str(payload.elevator_id),
                    "credential_id": payload.credential_id,
                    "is_dashboard_access": payload.is_dashboard_access,
                    "violation": "NOT_DASHBOARD_ACCESS",
                },
                message="Dashboard card swipe requires is_dashboard_access=True.",
                credential_id=payload.credential_id,
                db_pool=db_pool,
            )

        try:
            async with db_pool.transaction() as conn:
                return await DashboardAccessControl._validate_dashboard_swipe(
                    conn, payload, ts, db_pool
                )
        except Exception:
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_emergency_override(
        payload: EmergencyOverridePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not EmergencyOverrideType.has_value(payload.override_type):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.target_car_id or payload.incident_id or "UNKNOWN"),
                        source="EmergencyOverridePayload",
                        payload_details={"override_type": str(payload.override_type)},
                        message=f"Invalid override_type '{payload.override_type}'.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                target_entity = str(payload.target_car_id or payload.incident_id or UserRole.SYSTEM)
                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    role_check=UserRole.is_admin,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required.",
                    unauth_msg=f"Role '{{role}}' cannot execute emergency override '{payload.override_type}'.",
                    target_id=target_entity,
                    operation=str(payload.override_type),
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

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
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_rescue_car_dispatch(
        payload: RescueCarDispatchPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    role_check=UserRole.is_admin,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required.",
                    unauth_msg="Role '{role}' is not authorized to dispatch a rescue car.",
                    target_id=str(payload.rescue_car_id),
                    operation=AuditActionType.RESCUE_CAR_DISPATCHED.value,
                    extra_payload={"incident_id": payload.incident_id},
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

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
            return fail_closed(payload.credential_id)

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

        allowed_floors = parse_allowed_floors(
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
                    DashboardAccessControl.VISITOR_PASS_DEFAULT_DURATION_SECONDS,
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
                admin_record, denial = await authorize_credential_role(
                    conn,
                    payload.admin_credential_id,
                    ts,
                    role_check=UserRole.is_admin,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required.",
                    unauth_msg="Role '{role}' is not authorized to manage credentials.",
                    target_id=payload.target_credential_id or payload.target_user_id or "UNKNOWN",
                    operation=str(payload.action),
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert admin_record is not None
                admin_role, admin_user_id = admin_record["user_role"], admin_record["user_id"]

                if payload.action in (CredentialAction.ISSUE, CredentialAction.ISSUE_VISITOR_PASS):
                    return await DashboardAccessControl._issue_credential(
                        conn, payload, admin_record, ts, db_pool
                    )

                if not payload.target_credential_id:
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message=f"target_credential_id is required for action '{payload.action}'.",
                        credential_id=payload.admin_credential_id,
                    )

                target_record = await fetch_and_refresh_credential(
                    conn, payload.target_credential_id, ts, db_pool=db_pool
                )
                if target_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Target credential '{payload.target_credential_id}' not found.",
                        credential_id=payload.admin_credential_id,
                    )

                peer_denial = await DashboardAccessControl._check_peer_admin_restriction(
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

                return await DashboardAccessControl._modify_credential_status(
                    conn, payload, admin_record, target_record, ts, db_pool
                )
        except Exception:
            return fail_closed(payload.admin_credential_id)

    @staticmethod
    async def replace_lost_card(
        payload: RevokeAndReissuePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Atomic Revoke & Re-issue workflow (SEC-2.17, VT-17)."""
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                admin_record, denial = await authorize_credential_role(
                    conn,
                    payload.admin_credential_id,
                    ts,
                    role_check=UserRole.is_admin,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required to replace a lost card.",
                    unauth_msg="Role '{role}' is not authorized to replace lost credentials.",
                    target_id=payload.lost_credential_id,
                    operation=AuditActionType.CREDENTIAL_REPLACED.value,
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert admin_record is not None
                admin_role, admin_user_id = admin_record["user_role"], admin_record["user_id"]

                lost_record = await fetch_and_refresh_credential(
                    conn, payload.lost_credential_id, ts, db_pool=db_pool
                )
                if lost_record is None:
                    return AuthorizationResult.deny(
                        DenialReason.UNREGISTERED_CARD,
                        message=f"Lost credential '{payload.lost_credential_id}' does not exist.",
                        credential_id=payload.admin_credential_id,
                    )

                peer_denial = await DashboardAccessControl._check_peer_admin_restriction(
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

                allowed_floors = parse_allowed_floors(
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
            return fail_closed(payload.admin_credential_id)

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
                admin_record, denial = await authorize_credential_role(
                    conn,
                    admin_credential_id,
                    ts,
                    role_check=UserRole.is_admin,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required for audit integrity check.",
                    unauth_msg="Role '{role}' cannot run audit integrity check.",
                    target_id="audit_log_chain",
                    operation=AuditActionType.INTEGRITY_CHECK_RUN.value,
                    db_pool=db_pool,
                )
                if denial:
                    return denial, None
                assert admin_record is not None
                role = admin_record["user_role"]

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
            return fail_closed(admin_credential_id), None

    @staticmethod
    async def validate_dashboard_operation(
        payload: DashboardOperationPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """
        Validate an active 15-minute dashboard session with JIT lazy eviction,
        sliding TTL renewal, and role entitlement checks.
        """
        ts = Logging.normalize_timestamp(payload.timestamp)

        if not DashboardOperationType.has_value(payload.op_type):
            return await reject_malformed_msg(
                timestamp=ts,
                target_id="DASHBOARD",
                source="DashboardOperationPayload",
                payload_details={"op_type": str(payload.op_type), "violation": "UNKNOWN_DASHBOARD_OP"},
                message=f"Unknown dashboard operation '{payload.op_type}'.",
                db_pool=db_pool,
            )

        session = DashboardAccessControl.active_dashboard_sessions.get(payload.session_token)
        if not session:
            return AuthorizationResult.deny(
                DenialReason.SESSION_INVALID,
                message="No active dashboard session found.",
            )

        if ts > session["expires_at"]:
            del DashboardAccessControl.active_dashboard_sessions[payload.session_token]
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

        session["expires_at"] = ts + timedelta(
            seconds=DashboardAccessControl.DASHBOARD_SESSION_TIMEOUT_SECONDS
        )
        session["last_active_at"] = ts

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
                return fail_closed(session["credential_id"])
            return AuthorizationResult.deny(
                DenialReason.UNAUTHORIZED_ROLE,
                message=f"Role '{user_role}' is not authorized to execute '{op_enum.value}'.",
                credential_id=session["credential_id"],
            )

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
        with `reason = 'MANUAL'`.
        """
        ts = Logging.normalize_timestamp(timestamp or datetime.now())
        session = DashboardAccessControl.active_dashboard_sessions.pop(session_token, None)
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
            return fail_closed(session["credential_id"])

        return AuthorizationResult.allow(
            message="Dashboard session logged out.",
            credential_id=session["credential_id"],
            details={"reason": "MANUAL", "session_token": session_token},
        )

