from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
import uuid

from acs.enums import (
    DASHBOARD_PERMISSIONS,
    AuditActionType,
    DashboardOperationType,
    DenialReason,
    UserRole,
)
from acs.helpers import (
    authorize_credential_role,
    fail_closed,
    reject_malformed_msg,
    reset_shared_state,
    verify_swipe_credential,
)
from acs.logs import Logging
from acs.payloads import (
    AuditEntry,
    AuthorizationResult,
    CardSwipePayload,
    DashboardOperationPayload,
    IntegrityCheckResult,
)
from db import DbConnectionPool


class DashboardAccessControl:
    """
    ACS.DashboardAccessControl submodule (`acs/dashboard_accesscontrol.py`).
    Validates authentication, session lifecycle, and role/permission authorization
    for the SREMS management dashboard (dashboard card login, 15-minute sliding-TTL
    session operations, explicit session logout, and audit chain integrity checks).
    """

    DASHBOARD_SESSION_TIMEOUT_SECONDS: int = 15 * 60  # 15 minutes (`OR-19, SEC-2.15`)

    active_dashboard_sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def reset_in_memory_state() -> None:
        """Clear all dashboard session tokens and shared anomaly detection windows."""
        DashboardAccessControl.active_dashboard_sessions.clear()
        reset_shared_state()

    @staticmethod
    async def validate_card_swipe(
        payload: CardSwipePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Validate a card swipe on the dashboard login reader and establish a 15-minute session."""
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
                record, denial = await verify_swipe_credential(conn, payload, ts, db_pool)
                if denial is not None:
                    return denial
                assert record is not None

                user_role = record["user_role"]
                if user_role not in (UserRole.ADMIN, UserRole.TECHNICIAN):
                    await Logging.write_audit(
                        [
                            AuditEntry(
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
                            )
                        ],
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
                    [
                        AuditEntry(
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
                        )
                    ],
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
        except Exception:
            return fail_closed(payload.credential_id)

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
                    [
                        AuditEntry(
                            timestamp=ts,
                            actor_id=session["credential_id"],
                            actor_role=session["role"],
                            action_type=AuditActionType.ADMIN_LOGOUT,
                            target_id="DASHBOARD",
                            event_payload={
                                "reason": "TIMEOUT",
                                "session_token": payload.session_token,
                            },
                        )
                    ],
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
                    [
                        AuditEntry(
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
                        )
                    ],
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
                [
                    AuditEntry(
                        timestamp=ts,
                        actor_id=session["credential_id"],
                        actor_role=session["role"],
                        action_type=AuditActionType.ADMIN_LOGOUT,
                        target_id="DASHBOARD",
                        event_payload={
                            "reason": "MANUAL",
                            "session_token": session_token,
                        },
                    )
                ],
                db_pool=db_pool,
            )
        except Exception:
            return fail_closed(session["credential_id"])

        return AuthorizationResult.allow(
            message="Dashboard session logged out.",
            credential_id=session["credential_id"],
            details={"reason": "MANUAL", "session_token": session_token},
        )
