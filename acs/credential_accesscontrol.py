from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Optional
import uuid

import asyncpg

from acs.enums import (
    ROLE_TO_CREDENTIAL_TYPE,
    AuditActionType,
    CredentialAction,
    CredentialStatus,
    CredentialType,
    DenialReason,
    SuspensionReason,
    UserRole,
)
from acs.helpers import (
    LOST_CARD_REVIEW_WINDOW_SECONDS,
    authorize_credential_role,
    fail_closed,
    fetch_and_refresh_credential,
    parse_allowed_floors,
)
from acs.logs import Logging
from acs.payloads import (
    AuthorizationResult,
    CredentialManagementPayload,
    RevokeAndReissuePayload,
)
from db import DbConnectionPool


class CredentialAccessControl:
    """
    ACS.CredentialAccessControl submodule (`acs/credential_accesscontrol.py`).
    Validates and executes Admin credential lifecycle operations (`ISSUE`,
    `ISSUE_VISITOR_PASS`, `SUSPEND`, `REINSTATE`, `REVOKE`) and atomic lost-card
    replacement (`replace_lost_card`), enforcing peer-admin accountability.
    """

    VISITOR_PASS_DEFAULT_DURATION_SECONDS: int = 4 * 3600  # 4 hours

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
        if target_record["user_role"] == UserRole.ADMIN and (
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
    async def _insert_active_credential(
        conn: asyncpg.Connection,
        *,
        credential_id: str,
        user_id: str,
        credential_type: str,
        expires_at: Optional[datetime],
        allowed_floors: list[int],
        issued_by_admin: str,
        ts: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO credentials (
                credential_id, user_id, credential_type, status, suspension_reason,
                suspension_start, revocation_reason, revoked_at, expires_at,
                allowed_floors, issued_by_admin, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, NULL, NULL, NULL, NULL, $5, $6::jsonb, $7, $8, $8)
            """,
            credential_id,
            user_id,
            credential_type,
            CredentialStatus.ACTIVE.value,
            expires_at,
            json.dumps(allowed_floors),
            issued_by_admin,
            ts,
        )

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
                    CredentialAccessControl.VISITOR_PASS_DEFAULT_DURATION_SECONDS,
                )
            )
            expires_at = ts + timedelta(seconds=dur)

        await CredentialAccessControl._insert_active_credential(
            conn,
            credential_id=payload.target_credential_id,
            user_id=target_user_id,
            credential_type=cred_type,
            expires_at=expires_at,
            allowed_floors=allowed_floors,
            issued_by_admin=admin_record["user_id"],
            ts=ts,
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
        target_id = str(payload.target_credential_id)
        target_status = target_record["status"]
        admin_id = payload.admin_credential_id
        admin_role = admin_record["user_role"]
        details = payload.payload_details or {}

        if action == CredentialAction.SUSPEND:
            allowed_from = (CredentialStatus.ACTIVE,)
            reason = (
                str(details.get("suspension_reason", SuspensionReason.LOST.value))
                if details.get("suspension_reason") in SuspensionReason._value2member_map_
                else SuspensionReason.ADMIN_MANUAL.value
            )
            sql = "UPDATE credentials SET status = $2, suspension_reason = $3, suspension_start = $4, updated_at = $4 WHERE credential_id = $1"
            sql_args: tuple[Any, ...] = (
                target_id,
                CredentialStatus.SUSPENDED.value,
                reason,
                ts,
            )
            audit_action = AuditActionType.CREDENTIAL_SUSPENDED
            audit_payload: dict[str, Any] = {
                "credential_id": target_id,
                "reason": reason,
                "window_hours": 72 if reason == SuspensionReason.LOST else None,
            }
            ok_msg = f"Credential '{target_id}' suspended ({reason})."
        elif action == CredentialAction.REINSTATE:
            allowed_from = (CredentialStatus.SUSPENDED,)
            sql = "UPDATE credentials SET status = $2, suspension_reason = NULL, suspension_start = NULL, updated_at = $3 WHERE credential_id = $1"
            sql_args = (target_id, CredentialStatus.ACTIVE.value, ts)
            audit_action = AuditActionType.CREDENTIAL_REINSTATED
            audit_payload = {
                "credential_id": target_id,
                "notes": details.get("notes", "Identity verified by Admin."),
            }
            ok_msg = f"Credential '{target_id}' reinstated to ACTIVE."
        elif action == CredentialAction.REVOKE:
            allowed_from = (CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED)
            reason = str(details.get("revocation_reason", "ADMIN_ACTION"))
            sql = "UPDATE credentials SET status = $2, revocation_reason = $3, revoked_at = $4, updated_at = $4 WHERE credential_id = $1"
            sql_args = (target_id, CredentialStatus.REVOKED.value, reason, ts)
            audit_action = AuditActionType.CREDENTIAL_REVOKED
            audit_payload = {"credential_id": target_id, "trigger": reason}
            ok_msg = f"Credential '{target_id}' permanently revoked."
        else:
            return AuthorizationResult.deny(
                DenialReason.MALFORMED_MSG_REJECTED,
                message=f"Unsupported credential management action '{action}'.",
                credential_id=admin_id,
            )

        if target_status not in allowed_from:
            return AuthorizationResult.deny(
                DenialReason.INVALID_STATE_TRANSITION,
                message=f"Invalid credential state transition for action '{action}' from status '{target_status}'.",
                credential_id=admin_id,
            )

        await conn.execute(sql, *sql_args)
        await Logging.write_audit(
            timestamp=ts,
            actor_id=admin_id,
            actor_role=admin_role,
            action_type=audit_action,
            target_id=target_id,
            event_payload=audit_payload,
            db_pool=db_pool,
        )
        return AuthorizationResult.allow(message=ok_msg, credential_id=target_id)

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
                    return await CredentialAccessControl._issue_credential(
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

                peer_denial = await CredentialAccessControl._check_peer_admin_restriction(
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

                return await CredentialAccessControl._modify_credential_status(
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

                peer_denial = await CredentialAccessControl._check_peer_admin_restriction(
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
                await CredentialAccessControl._insert_active_credential(
                    conn,
                    credential_id=payload.replacement_credential_id,
                    user_id=lost_record["user_id"],
                    credential_type=lost_record["credential_type"],
                    expires_at=lost_record["expires_at"],
                    allowed_floors=allowed_floors,
                    issued_by_admin=admin_user_id,
                    ts=ts,
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
    async def apply_bulk_transitions(
        current_time: datetime,
        *,
        conn: Optional[asyncpg.Connection] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> dict[str, list[str]]:
        """
        Bulk-transition expired ACTIVE credentials (`EXPIRED`) and 72-hour LOST
        SUSPENDED credentials (`REVOKED`) using SQL `IN` updates and per-credential
        audit entries over a single connection.
        """
        ts = Logging.normalize_timestamp(current_time)
        review_cutoff = ts - timedelta(seconds=LOST_CARD_REVIEW_WINDOW_SECONDS)

        async def _run(active_conn: asyncpg.Connection) -> dict[str, list[str]]:
            async with active_conn.transaction():
                expired_rows = await active_conn.fetch(
                    """
                    SELECT credential_id, created_at, expires_at
                    FROM credentials
                    WHERE status = $1
                      AND expires_at IS NOT NULL
                      AND expires_at <= $2
                    ORDER BY credential_id ASC
                    FOR UPDATE
                    """,
                    CredentialStatus.ACTIVE.value,
                    ts,
                )
                expired_ids = [str(row["credential_id"]) for row in expired_rows]
                if expired_ids:
                    in_placeholders = ", ".join(
                        f"${idx}" for idx in range(3, 3 + len(expired_ids))
                    )
                    await active_conn.execute(
                        f"""
                        UPDATE credentials
                        SET status = $1,
                            updated_at = $2
                        WHERE credential_id IN ({in_placeholders})
                        """,
                        CredentialStatus.EXPIRED.value,
                        ts,
                        *expired_ids,
                    )
                    for row in expired_rows:
                        cid = str(row["credential_id"])
                        expires_at_dt = Logging.normalize_timestamp(row["expires_at"])
                        created_at_dt = Logging.normalize_timestamp(row["created_at"])
                        await Logging.write_audit(
                            timestamp=ts,
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
                            conn=active_conn,
                        )

                revoked_rows = await active_conn.fetch(
                    """
                    SELECT credential_id, suspension_start
                    FROM credentials
                    WHERE status = $1
                      AND suspension_reason = $2
                      AND suspension_start IS NOT NULL
                      AND suspension_start <= $3
                    ORDER BY credential_id ASC
                    FOR UPDATE
                    """,
                    CredentialStatus.SUSPENDED.value,
                    SuspensionReason.LOST.value,
                    review_cutoff,
                )
                revoked_ids = [str(row["credential_id"]) for row in revoked_rows]
                if revoked_ids:
                    in_placeholders = ", ".join(
                        f"${idx}" for idx in range(4, 4 + len(revoked_ids))
                    )
                    await active_conn.execute(
                        f"""
                        UPDATE credentials
                        SET status = $1,
                            revocation_reason = 'WINDOW_EXPIRED',
                            revoked_at = $2,
                            updated_at = $3
                        WHERE credential_id IN ({in_placeholders})
                        """,
                        CredentialStatus.REVOKED.value,
                        ts,
                        ts,
                        *revoked_ids,
                    )
                    for row in revoked_rows:
                        cid = str(row["credential_id"])
                        suspension_start_dt = Logging.normalize_timestamp(
                            row["suspension_start"]
                        )
                        await Logging.write_audit(
                            timestamp=ts,
                            actor_id=UserRole.SYSTEM,
                            actor_role=UserRole.SYSTEM,
                            action_type=AuditActionType.CREDENTIAL_REVOKED,
                            target_id=cid,
                            event_payload={
                                "credential_id": cid,
                                "trigger": "WINDOW_EXPIRED",
                                "suspension_start": Logging.format_timestamp(
                                    suspension_start_dt
                                ),
                                "revoked_at": Logging.format_timestamp(ts),
                            },
                            conn=active_conn,
                        )

                return {"expired": expired_ids, "revoked": revoked_ids}

        if conn is not None:
            return await _run(conn)

        async with db_pool.acquire() as acquired_conn:
            return await _run(acquired_conn)

