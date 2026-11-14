from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from acs.credential_accesscontrol import CredentialAccessControl
from acs.enums import CredentialAction, DashboardOperationType, UserRole
from acs.payloads import (
    AuthorizationResult,
    CredentialManagementPayload,
    RevokeAndReissuePayload,
)
from api.helpers import (
    auth_denial_response,
    auth_result_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock

router = APIRouter(prefix="/credentials", tags=["acs-credentials"])


class CredentialManageRequest(BaseModel):
    """Admin credential lifecycle action payload (`CredentialAccessControl.manage_credential`)."""

    action: CredentialAction | str
    target_user_id: Optional[str] = None
    target_credential_id: Optional[str] = None
    payload_details: dict[str, Any] = Field(default_factory=dict)


class ReplaceLostCardRequest(BaseModel):
    """Atomic lost-card revoke and replacement payload (`CredentialAccessControl.replace_lost_card`)."""

    lost_credential_id: str
    replacement_credential_id: str
    reason: str = "LOST_AND_REPLACED"


@router.post("/manage", response_model=AuthorizationResult)
async def manage_credential(
    body: CredentialManageRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Issue, suspend, reinstate, revoke, or create a visitor pass using the Admin's Bearer session."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MANAGE_CREDENTIALS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result = await CredentialAccessControl.manage_credential(
        CredentialManagementPayload(
            admin_credential_id=session.credential_id,
            action=body.action,
            target_user_id=body.target_user_id,
            target_credential_id=body.target_credential_id,
            payload_details=body.payload_details,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.post("/replace-lost", response_model=AuthorizationResult)
async def replace_lost_card(
    body: ReplaceLostCardRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Atomically revoke a lost card and issue a replacement card using the Admin's Bearer session."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MANAGE_CREDENTIALS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result = await CredentialAccessControl.replace_lost_card(
        RevokeAndReissuePayload(
            admin_credential_id=session.credential_id,
            lost_credential_id=body.lost_credential_id,
            replacement_credential_id=body.replacement_credential_id,
            reason=body.reason,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.get("")
async def list_credentials(
    search: Optional[str] = None,
    status: Optional[str] = None,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """List and search credentials and registered users (`SEC-2.8`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MANAGE_CREDENTIALS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    clauses: list[str] = []
    params: list[Any] = []
    if status and status.upper() != "ALL":
        params.append(status.upper())
        clauses.append(f"c.status = ${len(params)}")
    if search and search.strip():
        params.append(f"%{search.strip()}%")
        idx = len(params)
        clauses.append(
            f"(c.credential_id ILIKE ${idx} OR COALESCE(u.full_name, '') ILIKE ${idx} OR COALESCE(u.role, '') ILIKE ${idx} OR COALESCE(u.apartment_unit, '') ILIKE ${idx})"
        )

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT
            c.credential_id AS card_id,
            c.credential_id,
            c.user_id,
            u.full_name,
            u.role,
            u.apartment_unit,
            u.assigned_floor,
            c.credential_type,
            c.status,
            c.suspension_reason,
            c.allowed_floors,
            (c.credential_type = 'VISITOR') AS is_visitor_pass,
            c.issued_by_admin AS host_user_id,
            c.created_at AS issued_at,
            c.expires_at,
            c.suspension_start AS suspended_at,
            c.revoked_at
        FROM credentials c
        LEFT JOIN users u ON u.user_id = c.user_id
        {where_sql}
        ORDER BY c.created_at DESC, c.credential_id ASC
    """
    async with db_pool.acquire() as conn:
        cred_rows = await conn.fetch(query, *params)
        user_rows = await conn.fetch(
            """
            SELECT user_id, full_name, role, apartment_unit, assigned_floor, created_at
            FROM users
            ORDER BY full_name ASC
            """
        )

    import json

    items: list[dict[str, Any]] = []
    for r in cred_rows:
        d = dict(r)
        if isinstance(d.get("allowed_floors"), str):
            try:
                d["allowed_floors"] = json.loads(d["allowed_floors"])
            except Exception:
                d["allowed_floors"] = []
        items.append(d)
    users = [dict(r) for r in user_rows]
    return {"items": items, "users": users, "count": len(items)}


class CreateUserBody(BaseModel):
    full_name: str
    role: UserRole | str
    apartment_unit: Optional[str] = None
    assigned_floor: Optional[int] = None


@router.post("/users", status_code=201)
async def create_user_record(
    body: CreateUserBody,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Create a user profile so credentials can be issued to them (`SEC-2.8`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MANAGE_CREDENTIALS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    now = SimulationClock.now()
    role_val = str(body.role.value if hasattr(body.role, "value") else body.role)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (full_name, role, apartment_unit, assigned_floor, created_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING user_id::text AS user_id, full_name, role, apartment_unit, assigned_floor, created_at
            """,
            body.full_name.strip(),
            role_val,
            body.apartment_unit,
            body.assigned_floor,
            now,
        )
    return {"user": dict(row)}


