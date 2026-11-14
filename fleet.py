from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from acs.enums import (
    DashboardOperationType,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
)
from acs.fleet_accesscontrol import FleetAccessControl
from acs.payloads import (
    AuthorizationResult,
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
)
from api.helpers import (
    auth_denial_response,
    auth_result_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool
from simulation.operational import operational_simulator

router = APIRouter(prefix="/fleet", tags=["acs-fleet"])


class MaintenanceOperationRequest(BaseModel):
    """Maintenance isolation or diagnostic request (`FleetAccessControl.validate_maintenance_operation`)."""

    elevator_id: ElevatorId | str
    operation_type: MaintenanceOperationType | str


class ReturnToServiceRequest(BaseModel):
    """Return-to-service clearance request (`FleetAccessControl.validate_return_to_service`)."""

    elevator_id: ElevatorId | str
    technician_confirmation: bool


class EmergencyOverrideRequest(BaseModel):
    """Admin safety override request (`FleetAccessControl.validate_emergency_override`)."""

    override_type: EmergencyOverrideType | str
    target_car_id: Optional[ElevatorId | str] = None
    incident_id: Optional[str] = None
    reason: str


class RescueDispatchRequest(BaseModel):
    """Rescue car dispatch request (`FleetAccessControl.validate_rescue_car_dispatch`)."""

    incident_id: str
    rescue_car_id: ElevatorId | str
    incident_floor: int
    dwell_wait_seconds: int = 120


@router.post("/maintenance", response_model=AuthorizationResult)
async def execute_maintenance_operation(
    body: MaintenanceOperationRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Authorize and validate a maintenance operation (`ADMIN` or `TECHNICIAN`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result = await FleetAccessControl.validate_maintenance_operation(
        MaintenanceOperationPayload(
            credential_id=session.credential_id,
            elevator_id=body.elevator_id,
            operation_type=body.operation_type,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if result.allowed and str(body.operation_type) == "ENTER_MAINTENANCE":
        operational_simulator.isolate(str(body.elevator_id), "MAINTENANCE")
    return auth_result_response(result)


@router.post("/return-to-service", response_model=AuthorizationResult)
async def return_to_service(
    body: ReturnToServiceRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Authorize and validate returning a car from maintenance to service (`ADMIN` or `TECHNICIAN`)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    # Recovery conditions must be checked against current test-stub state.
    # Authorization alone does not restore operational availability.
    if ElevatorId.has_value(body.elevator_id) and not operational_simulator.recoverable(str(body.elevator_id)):
        return auth_result_response(AuthorizationResult.deny(
            "INVALID_STATE_TRANSITION",
            message="Return to service blocked: stale state, active fault/emergency or obstruction.",
        ))

    result = await FleetAccessControl.validate_return_to_service(
        ReturnToServicePayload(
            credential_id=session.credential_id,
            elevator_id=body.elevator_id,
            technician_confirmation=body.technician_confirmation,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if result.allowed:
        operational_simulator.isolate(str(body.elevator_id), "IN_SERVICE")
    return auth_result_response(result)


@router.post("/emergency-override", response_model=AuthorizationResult)
async def emergency_override(
    body: EmergencyOverrideRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Authorize and validate an Admin-only emergency override command."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=body.override_type,
            target_car_id=body.target_car_id,
            incident_id=body.incident_id,
            reason=body.reason,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.post("/rescue-dispatch", response_model=AuthorizationResult)
async def rescue_dispatch(
    body: RescueDispatchRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Authorize and validate an Admin-only rescue car dispatch."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    result = await FleetAccessControl.validate_rescue_car_dispatch(
        RescueCarDispatchPayload(
            credential_id=session.credential_id,
            incident_id=body.incident_id,
            rescue_car_id=body.rescue_car_id,
            incident_floor=body.incident_floor,
            dwell_wait_seconds=body.dwell_wait_seconds,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.get("/view", response_model=AuthorizationResult)
async def authorize_fleet_view(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Verify dashboard permission to view fleet status (`ADMIN` or `TECHNICIAN`)."""
    auth_res, _ = await authorize_dashboard_op(
        DashboardOperationType.VIEW_FLEET_STATUS, session_token, db_pool
    )
    return auth_result_response(auth_res)


@router.get("/diagnostics", response_model=AuthorizationResult)
async def authorize_diagnostics_view(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Verify dashboard permission to view diagnostics (`ADMIN` or `TECHNICIAN`)."""
    auth_res, _ = await authorize_dashboard_op(
        DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool
    )
    return auth_result_response(auth_res)

