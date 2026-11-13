from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from acs.credential_accesscontrol import CredentialAccessControl
from acs.enums import CredentialAction, DashboardOperationType
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

