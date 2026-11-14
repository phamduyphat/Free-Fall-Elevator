from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
import uuid

import asyncpg

from acs.enums import (
    AlertSeverity,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    CredentialStatus,
    DenialReason,
    ElevatorId,
    SuspensionReason,
    UserRole,
)
from acs.helpers import (
    fail_closed,
    parse_allowed_floors,
    record_failed_attempt,
    reject_malformed_msg,
    reject_unauthorized_role,
    reset_shared_state,
    verify_swipe_credential,
)
from acs.logs import Logging
from acs.payloads import (
    AuditEntry,
    AuthorizationResult,
    CarDestinationRequestPayload,
    CardSwipePayload,
    E5ServiceRunPayload,
)
from db import DbConnectionPool


class CabAccessControl:
    """
    ACS.CabAccessControl submodule (`acs/cab_accesscontrol.py`).
    Validates authentication and role/floor authorization for in-cabin elevator
    operations (card swipes, destination selection, and E5 freight runs),
    manages single-use per-car presentation tokens, and detects cloned
    credentials across elevator cars.
    """

    RESIDENT_VISITOR_WINDOW_SECONDS: int = 5
    STAFF_TECH_WINDOW_SECONDS: int = 30
    CLONE_DETECTION_WINDOW_SECONDS: int = 10

    active_car_sessions: dict[str, dict[str, Any]] = {}
    recent_presentations_window: list[dict[str, Any]] = []

    @staticmethod
    def reset_in_memory_state() -> None:
        """Clear all in-cabin session tokens and anomaly detection windows."""
        CabAccessControl.active_car_sessions.clear()
        CabAccessControl.recent_presentations_window.clear()
        reset_shared_state()

    @staticmethod
    async def _handle_if_cloned_swipe(
        conn: asyncpg.Connection,
        payload: CardSwipePayload,
        ts: datetime,
        db_pool: type[DbConnectionPool],
    ) -> bool:
        clone_window = timedelta(seconds=CabAccessControl.CLONE_DETECTION_WINDOW_SECONDS)
        CabAccessControl.recent_presentations_window = [
            e
            for e in CabAccessControl.recent_presentations_window
            if (ts - e["timestamp"]) < clone_window
        ]
        conflicting = next(
            (
                e
                for e in CabAccessControl.recent_presentations_window
                if e["credential_id"] == payload.credential_id
                and e["car_id"] != payload.elevator_id
                and timedelta(0) <= (ts - e["timestamp"]) < clone_window
            ),
            None,
        )
        CabAccessControl.recent_presentations_window.append(
            {
                "credential_id": payload.credential_id,
                "car_id": str(payload.elevator_id),
                "timestamp": ts,
            }
        )
        if conflicting is None:
            return False

        prior_car = conflicting["car_id"]
        prior_session = CabAccessControl.active_car_sessions.get(prior_car)
        if prior_session and prior_session.get("credential_id") == payload.credential_id:
            CabAccessControl.active_car_sessions.pop(prior_car, None)
        CabAccessControl.active_car_sessions.pop(str(payload.elevator_id), None)

        updated_id = await conn.fetchval(
            """
            UPDATE credentials
            SET status = $2, suspension_reason = $3, suspension_start = $4, updated_at = $4
            WHERE credential_id = $1
            RETURNING credential_id
            """,
            payload.credential_id,
            CredentialStatus.SUSPENDED.value,
            SuspensionReason.CLONE_SUSPECTED.value,
            ts,
        )
        if updated_id is not None:
            await Logging.write_audit(
                [
                    AuditEntry(
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
                    )
                ],
                db_pool=db_pool,
            )

        await Logging.record_security_incident(
            timestamp=ts,
            credential_id=payload.credential_id,
            actor_role=UserRole.SYSTEM,
            target_id=str(payload.elevator_id),
            action_type=AuditActionType.ACCESS_DENIAL,
            event_payload={
                "elevator_id": str(payload.elevator_id),
                "secondary_car_id": prior_car,
                "origin_floor": payload.origin_floor,
                "denial_reason": SuspensionReason.CLONE_SUSPECTED.value,
            },
            elevator_id=str(payload.elevator_id),
            origin_floor=payload.origin_floor,
            destination_floor=payload.origin_floor,
            denial_reason=DenialReason.CARD_SUSPENDED,
            alert_triggered=True,
            alert_severity=AlertSeverity.CRITICAL,
            alert_type=AlertType.CLONED_CREDENTIAL,
            alert_details=(
                f"Cloned credential '{payload.credential_id}' tapped in {payload.elevator_id} "
                f"and {prior_car} within < {CabAccessControl.CLONE_DETECTION_WINDOW_SECONDS}s."
            ),
            primary_car_id=str(payload.elevator_id),
            secondary_car_id=prior_car,
            db_pool=db_pool,
        )
        return True

    @staticmethod
    async def validate_card_swipe(
        payload: CardSwipePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Validate an in-cabin card swipe, detect cloning, and issue a per-car token."""
        ts = Logging.normalize_timestamp(payload.timestamp)

        if payload.is_dashboard_access or not ElevatorId.has_value(payload.elevator_id):
            return await reject_malformed_msg(
                timestamp=ts,
                target_id=str(payload.elevator_id),
                source="CardSwipePayload",
                payload_details={
                    "elevator_id": str(payload.elevator_id),
                    "credential_id": payload.credential_id,
                    "is_dashboard_access": payload.is_dashboard_access,
                    "violation": "INVALID_ELEVATOR_ID",
                },
                message=f"Invalid in-cabin swipe payload for elevator_id '{payload.elevator_id}'.",
                credential_id=payload.credential_id,
                db_pool=db_pool,
            )

        try:
            async with db_pool.transaction() as conn:
                if await CabAccessControl._handle_if_cloned_swipe(conn, payload, ts, db_pool):
                    return AuthorizationResult.deny(
                        DenialReason.CARD_SUSPENDED,
                        message=f"Cloned credential detected on {payload.elevator_id}; card suspended.",
                        credential_id=payload.credential_id,
                        alert_triggered=True,
                        details={
                            "suspension_reason": SuspensionReason.CLONE_SUSPECTED.value,
                        },
                    )

                record, denial = await verify_swipe_credential(conn, payload, ts, db_pool)
                if denial is not None:
                    return denial
                assert record is not None

                user_role = record["user_role"]
                allowed_floors = parse_allowed_floors(
                    record["allowed_floors"],
                    role=user_role,
                    assigned_floor=record.get("assigned_floor"),
                )
                timeout_sec = (
                    CabAccessControl.STAFF_TECH_WINDOW_SECONDS
                    if user_role in (UserRole.BUILDING_SERVICE, UserRole.TECHNICIAN)
                    else CabAccessControl.RESIDENT_VISITOR_WINDOW_SECONDS
                )
                token = str(uuid.uuid4())
                expires_at = ts + timedelta(seconds=timeout_sec)
                CabAccessControl.active_car_sessions[str(payload.elevator_id)] = {
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
        except Exception:
            return fail_closed(payload.credential_id)

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
            alert_fired = await record_failed_attempt(
                credential_id=cred_id,
                car_id=str(payload.elevator_id),
                timestamp=ts,
                reason=reason,
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

        await Logging.record_security_incident(
            timestamp=ts,
            credential_id=cred_id,
            actor_role=role,
            target_id=str(payload.elevator_id),
            action_type=action_type,
            event_payload=event_payload,
            elevator_id=str(payload.elevator_id),
            origin_floor=payload.origin_floor,
            destination_floor=payload.requested_floor,
            denial_reason=reason,
            alert_triggered=alert_fired,
            db_pool=db_pool,
        )
        return AuthorizationResult.deny(
            reason,
            message=message,
            credential_id=cred_id,
            alert_triggered=alert_fired,
        )

    @staticmethod
    def _check_car_session(
        elevator_id: str,
        ts: datetime,
    ) -> tuple[
        Optional[dict[str, Any]],
        Optional[tuple[DenialReason, str, AuditActionType, Optional[float]]],
    ]:
        """
        Verify an active, unconsumed, unexpired in-cabin session for `elevator_id`.
        Returns `(session, None)` on success, or `(session_or_none, (reason, message, action_type, token_age))` on failure.
        """
        session = CabAccessControl.active_car_sessions.get(elevator_id)
        if session is None:
            return None, (
                DenialReason.NO_ACTIVE_CREDENTIAL,
                f"No active credential session in {elevator_id}.",
                AuditActionType.ACCESS_DENIAL,
                None,
            )
        if session.get("consumed", False):
            return session, (
                DenialReason.ONE_TAP_LIMIT_EXCEEDED,
                "Single-use tap token already consumed; please re-tap credential.",
                AuditActionType.ONE_TAP_VIOLATION,
                (ts - session["granted_at"]).total_seconds(),
            )
        if ts > session["expires_at"]:
            return session, (
                DenialReason.TOKEN_WINDOW_EXPIRED,
                "Credential selection window expired; please re-tap credential.",
                AuditActionType.ACCESS_DENIAL,
                None,
            )
        return session, None

    @staticmethod
    async def validate_car_destination(
        payload: CarDestinationRequestPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        if not ElevatorId.has_value(payload.elevator_id) or not (
            payload.requested_floor == -1 or (1 <= payload.requested_floor <= 30)
        ):
            return await reject_malformed_msg(
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
                if payload.requested_floor == -1:
                    if payload.elevator_id == ElevatorId.E5:
                        session = CabAccessControl.active_car_sessions.get(ElevatorId.E5.value)
                        return await CabAccessControl._deny_destination_selection(
                            payload=payload,
                            cred_id=session["credential_id"] if session else None,
                            role=session["role"] if session else UserRole.SYSTEM,
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

                session, session_err = CabAccessControl._check_car_session(
                    str(payload.elevator_id), ts
                )
                if session_err is not None:
                    reason, msg, action_type, token_age = session_err
                    return await CabAccessControl._deny_destination_selection(
                        payload=payload,
                        cred_id=session["credential_id"] if session else None,
                        role=session["role"] if session else UserRole.SYSTEM,
                        reason=reason,
                        message=msg,
                        action_type=action_type,
                        token_age_seconds=token_age,
                        record_failure=(session is not None),
                        ts=ts,
                        db_pool=db_pool,
                    )
                assert session is not None
                cred_id, role = session["credential_id"], session["role"]

                e5_forbidden = (
                    payload.elevator_id == ElevatorId.E5
                    and not UserRole.can_request_e5_freight(role)
                )
                floor_forbidden = payload.requested_floor not in session["allowed_floors"]

                if e5_forbidden or floor_forbidden:
                    return await CabAccessControl._deny_destination_selection(
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
            return fail_closed()

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
                        [
                            AuditEntry(
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
                            )
                        ],
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        reason,
                        message="E5 service run requires elevator_id='E5' and destination_floor in 1..30.",
                    )

                session, session_err = CabAccessControl._check_car_session(
                    ElevatorId.E5.value, ts
                )
                if session_err is not None:
                    reason, msg, action_type, token_age = session_err
                    cred_id = session["credential_id"] if session else None
                    if action_type == AuditActionType.ONE_TAP_VIOLATION and session:
                        await Logging.write_audit(
                            [
                                AuditEntry(
                                    timestamp=ts,
                                    actor_id=cred_id or UserRole.SYSTEM,
                                    actor_role=session["role"],
                                    action_type=AuditActionType.ONE_TAP_VIOLATION,
                                    target_id=ElevatorId.E5,
                                    event_payload={
                                        "elevator_id": ElevatorId.E5.value,
                                        "denied_floor": payload.destination_floor,
                                        "token_age_seconds": token_age,
                                    },
                                )
                            ],
                            db_pool=db_pool,
                        )
                    return AuthorizationResult.deny(reason, message=msg, credential_id=cred_id)

                assert session is not None
                cred_id, role = session["credential_id"], session["role"]
                required_roles = [
                    UserRole.BUILDING_SERVICE.value,
                    UserRole.TECHNICIAN.value,
                    UserRole.ADMIN.value,
                ]
                if role not in required_roles:
                    return await reject_unauthorized_role(
                        timestamp=ts,
                        actor_id=cred_id,
                        actor_role=role,
                        target_id=ElevatorId.E5.value,
                        operation=AuditActionType.E5_SERVICE_RUN_START.value,
                        required_roles=required_roles,
                        message=f"Role '{role}' is not authorized to initiate an E5 service run.",
                        extra_payload={
                            "destination_floor": payload.destination_floor,
                            "purpose": payload.purpose,
                        },
                        db_pool=db_pool,
                    )

                session["consumed"] = True
                await Logging.write_audit(
                    [
                        AuditEntry(
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
                        )
                    ],
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
            return fail_closed()
