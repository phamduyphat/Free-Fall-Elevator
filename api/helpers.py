from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from fastapi import Header, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import DashboardOperationType, DenialReason
from acs.payloads import AuthorizationResult, DashboardOperationPayload
from db import DbConnectionPool
from simulation.clock import SimulationClock

DENIAL_HTTP_STATUS: dict[str, int] = {
    DenialReason.MALFORMED_MSG_REJECTED.value: status.HTTP_400_BAD_REQUEST,
    DenialReason.CONFIRMATION_REQUIRED.value: status.HTTP_400_BAD_REQUEST,
    DenialReason.SESSION_INVALID.value: status.HTTP_401_UNAUTHORIZED,
    DenialReason.SESSION_EXPIRED.value: status.HTTP_401_UNAUTHORIZED,
    DenialReason.UNREGISTERED_CARD.value: status.HTTP_401_UNAUTHORIZED,
    DenialReason.NO_ACTIVE_CREDENTIAL.value: status.HTTP_401_UNAUTHORIZED,
    DenialReason.TOKEN_WINDOW_EXPIRED.value: status.HTTP_401_UNAUTHORIZED,
    DenialReason.UNAUTHORIZED_ROLE.value: status.HTTP_403_FORBIDDEN,
    DenialReason.UNAUTHORIZED_CMD_REJECTED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.DASHBOARD_ACCESS_DENIED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.PEER_ADMIN_REQUIRED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.UNAUTHORIZED_FLOOR.value: status.HTTP_403_FORBIDDEN,
    DenialReason.E5_B1_FORBIDDEN.value: status.HTTP_403_FORBIDDEN,
    DenialReason.CARD_SUSPENDED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.CARD_REVOKED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.CARD_EXPIRED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.ONE_TAP_LIMIT_EXCEEDED.value: status.HTTP_403_FORBIDDEN,
    DenialReason.INVALID_STATE_TRANSITION.value: status.HTTP_409_CONFLICT,
    DenialReason.CREDENTIAL_ALREADY_EXISTS.value: status.HTTP_409_CONFLICT,
    DenialReason.REPLACEMENT_ID_IN_USE.value: status.HTTP_409_CONFLICT,
    DenialReason.COMMAND_CONFLICT_REJECTED.value: status.HTTP_409_CONFLICT,
    DenialReason.DB_FAIL_CLOSED.value: status.HTTP_503_SERVICE_UNAVAILABLE,
}


@dataclass(frozen=True)
class DashboardSessionContext:
    """Server-side dashboard session metadata resolved from `DashboardAccessControl.active_dashboard_sessions`."""

    session_token: str
    credential_id: str
    user_id: str
    role: str
    timestamp: datetime


def denial_status_code(reason_code: Optional[DenialReason | str]) -> int:
    """Map an ACS `DenialReason` to a semantic HTTP error status code."""
    if reason_code is None:
        return status.HTTP_403_FORBIDDEN
    return DENIAL_HTTP_STATUS.get(str(reason_code), status.HTTP_403_FORBIDDEN)


def auth_denial_response(result: AuthorizationResult) -> JSONResponse:
    """Serialize a denied `AuthorizationResult` as the top-level JSON body with semantic status."""
    return JSONResponse(
        status_code=denial_status_code(result.reason_code),
        content=jsonable_encoder(asdict(result)),
    )


def auth_result_response(
    result: AuthorizationResult,
) -> AuthorizationResult | JSONResponse:
    """Return `200` with `AuthorizationResult` if allowed, or semantic HTTP error with full payload."""
    if result.allowed:
        return result
    return auth_denial_response(result)


def extract_bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    """Extract the opaque Bearer `session_token` from the HTTP `Authorization` header."""
    if not authorization:
        return ""
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


async def authorize_dashboard_op(
    op_type: DashboardOperationType,
    session_token: str,
    db_pool: type[DbConnectionPool],
) -> tuple[AuthorizationResult, Optional[DashboardSessionContext]]:
    """
    Validate the opaque Bearer token via `DashboardAccessControl.validate_dashboard_operation`
    at `SimulationClock.now()` and extract credential/user metadata from the server-side store.
    """
    now = SimulationClock.now()
    if not session_token:
        return (
            AuthorizationResult.deny(
                DenialReason.SESSION_INVALID,
                message="Authorization Bearer session token is required.",
            ),
            None,
        )

    auth_res = await DashboardAccessControl.validate_dashboard_operation(
        DashboardOperationPayload(
            session_token=session_token,
            op_type=op_type,
            timestamp=now,
        ),
        db_pool=db_pool,
    )
    if not auth_res.allowed:
        return auth_res, None

    session_entry = DashboardAccessControl.active_dashboard_sessions.get(session_token, {})
    cred_id = str(session_entry.get("credential_id") or auth_res.credential_id or "")
    user_id = str(session_entry.get("user_id") or auth_res.details.get("user_id") or "")
    role = str(session_entry.get("role") or auth_res.details.get("role") or "")
    return (
        auth_res,
        DashboardSessionContext(
            session_token=session_token,
            credential_id=cred_id,
            user_id=user_id,
            role=role,
            timestamp=now,
        ),
    )

