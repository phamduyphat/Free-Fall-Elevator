from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    DashboardOperationType,
    DenialReason,
    ElevatorId,
)
from acs.logs import Logging
from acs.payloads import AuthorizationResult
from api.helpers import (
    auth_denial_response,
    auth_result_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool

router = APIRouter(prefix="/logs", tags=["acs-logs"])


class AlertStatusUpdateRequest(BaseModel):
    """Request body for acknowledging or resolving a security alert."""

    new_status: AlertStatus | str


@router.get("/authorization-events", response_model=None)
async def search_authorization_events(
    credential_id: Optional[str] = None,
    elevator_id: Optional[ElevatorId | str] = None,
    outcome: Optional[AuthorizationOutcome | str] = None,
    denial_reason: Optional[DenialReason | str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=10000),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> dict[str, Any] | JSONResponse:
    """Search `access_authorization_events` by time range and filters (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_AUDIT_LOGS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    items = await Logging.search_authorization_events(
        credential_id=credential_id,
        elevator_id=elevator_id,
        outcome=outcome,
        denial_reason=denial_reason,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        db_pool=db_pool,
    )
    return {"items": items, "count": len(items)}


@router.get("/audit", response_model=None)
async def search_audit_logs(
    actor_id: Optional[str] = None,
    action_type: Optional[AuditActionType | str] = None,
    target_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=10000),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> dict[str, Any] | JSONResponse:
    """Search `audit_log_chain` by time range and filters (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_AUDIT_LOGS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    items = await Logging.search_audit_logs(
        actor_id=actor_id,
        action_type=action_type,
        target_id=target_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        db_pool=db_pool,
    )
    return {"items": items, "count": len(items)}


@router.get("/incidents", response_model=None)
async def search_incidents(
    incident_status: Optional[str] = Query(default=None, alias="status"),
    emergency_type: Optional[str] = None,
    elevator_id: Optional[ElevatorId | str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=10000),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> dict[str, Any] | JSONResponse:
    """Search `incidents` by time range and filters (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_AUDIT_LOGS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    items = await Logging.search_incidents(
        status=incident_status,
        emergency_type=emergency_type,
        elevator_id=elevator_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        db_pool=db_pool,
    )
    return {"items": items, "count": len(items)}


@router.get("/alerts", response_model=None)
async def search_security_alerts(
    alert_status: Optional[AlertStatus | str] = Query(default=None, alias="status"),
    severity: Optional[AlertSeverity | str] = None,
    alert_type: Optional[AlertType | str] = None,
    credential_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=10000),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> dict[str, Any] | JSONResponse:
    """Search `security_alerts` by time range and filters (`ADMIN` or `TECHNICIAN`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    items = await Logging.search_security_alerts(
        status=alert_status,
        severity=severity,
        alert_type=alert_type,
        credential_id=credential_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        db_pool=db_pool,
    )
    return {"items": items, "count": len(items)}


@router.patch("/alerts/{alert_id}")
async def update_security_alert_status(
    alert_id: str,
    body: AlertStatusUpdateRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> JSONResponse:
    """Acknowledge or resolve a security alert (`ADMIN` only under `EMERGENCY_OVERRIDE`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    try:
        updated = await Logging.update_security_alert_status(
            alert_id=alert_id,
            new_status=body.new_status,
            admin_user_id=session.user_id,
            timestamp=session.timestamp,
            db_pool=db_pool,
        )
    except ValueError as exc:
        return auth_denial_response(
            AuthorizationResult.deny(
                DenialReason.MALFORMED_MSG_REJECTED,
                message=str(exc),
                credential_id=session.credential_id,
            )
        )

    if not updated:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "updated": False,
                "alert_id": alert_id,
                "detail": "Security alert not found.",
            },
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "updated": True,
            "alert_id": alert_id,
            "new_status": str(body.new_status),
        },
    )


@router.post("/audit/verify", response_model=AuthorizationResult)
async def verify_audit_chain(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Run cryptographic hash chain verification on `audit_log_chain` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.RUN_AUDIT_INTEGRITY, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result, _ = await DashboardAccessControl.run_audit_integrity_check(
        admin_credential_id=session.credential_id,
        timestamp=session.timestamp,
        db_pool=db_pool,
    )
    return auth_result_response(result)
