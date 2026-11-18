"""Mode 1 operational API backed by a clearly labelled, in-memory test stub."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acs.enums import (
    DashboardOperationType,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
)
from acs.fleet_accesscontrol import FleetAccessControl
from acs.helpers import reject_malformed_msg
from acs.payloads import (
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    ReturnToServicePayload,
)
from api.helpers import (
    auth_denial_response,
    auth_result_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock
from simulation.operational import OperationalError, operational_simulator

router = APIRouter(prefix="/operational", tags=["operational-test-stub"])

_VALID_STUB_STATES = frozenset(
    {"IN_SERVICE", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"}
)


class HallCallInput(BaseModel):
    origin_floor: int
    call_type: str = "ORDINARY"
    direction: Optional[str] = None
    traffic_profile: Optional[str] = None


class StubAdvanceInput(BaseModel):
    seconds: int = 1


class StubStateInput(BaseModel):
    elevator_id: str
    state: str


@router.post("/hall-calls", status_code=201)
async def submit_hall_call(
    body: HallCallInput,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Unauthenticated passenger hall call, with duplicate merging, malformed-input audit, and E5 exceptions."""
    valid_floor = body.origin_floor == -1 or (1 <= body.origin_floor <= 30)
    valid_type = body.call_type in ("ORDINARY", "FREIGHT")
    valid_dir = (
        body.direction in ("UP", "DOWN")
        if body.call_type == "ORDINARY"
        else body.direction is None
    )
    if not (valid_floor and valid_type and valid_dir):
        denied = await reject_malformed_msg(
            timestamp=SimulationClock.now(),
            target_id="HALL_PANEL",
            source="HallCallInput",
            payload_details={
                "origin_floor": body.origin_floor,
                "call_type": body.call_type,
                "direction": body.direction,
            },
            message="Invalid hall-call floor, type, or direction.",
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    try:
        request, merged = operational_simulator.submit_hall_call(
            origin_floor=body.origin_floor,
            direction=body.direction,  # type: ignore[arg-type]
            call_type=body.call_type,  # type: ignore[arg-type]
            traffic_profile=body.traffic_profile,
        )
        return {"request": request, "merged": merged, "source": "labelled-test-stub"}
    except OperationalError as exc:
        raise HTTPException(
            status_code=409 if "unavailable" in str(exc) else 422,
            detail=str(exc),
        ) from exc


@router.get("/hall-calls")
async def list_hall_calls() -> dict:
    return {"source": "labelled-test-stub", "requests": operational_simulator.hall_call_status()}


@router.get("/hall-calls/{request_id}")
async def get_hall_call(request_id: str) -> dict:
    try:
        return {
            "source": "labelled-test-stub",
            "request": operational_simulator.hall_call_status(request_id),
        }
    except OperationalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/stub/advance")
async def advance_test_stub(
    body: StubAdvanceInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Admin-only deterministic stepping for local tests and reproducible demonstrations."""
    authorized, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(authorized)
    if not (0 <= body.seconds <= 10000):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id="SIMULATOR",
            source="StubAdvanceInput",
            payload_details={"seconds": body.seconds},
            message="seconds must be an integer between 0 and 10000.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    return operational_simulator.advance(body.seconds)


@router.post("/stub/state")
async def update_test_stub_car(
    body: StubStateInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Admin-only synthetic equipment-state event routed through FleetAccessControl and audit_log_chain."""
    authorized, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(authorized)

    if not ElevatorId.has_value(body.elevator_id) or body.state not in _VALID_STUB_STATES:
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="StubStateInput",
            payload_details={"elevator_id": body.elevator_id, "state": body.state},
            message="Invalid elevator_id or operational state.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    if body.state == "IN_SERVICE":
        if not operational_simulator.can_return_to_service(body.elevator_id):
            raise HTTPException(
                status_code=409,
                detail="Recovery checks failed: stale, fault, door, or unresolved incident.",
            )
        acs_res = await FleetAccessControl.validate_return_to_service(
            ReturnToServicePayload(
                credential_id=session.credential_id,
                elevator_id=body.elevator_id,
                technician_confirmation=True,
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )
    elif body.state == "MAINTENANCE":
        acs_res = await FleetAccessControl.validate_maintenance_operation(
            MaintenanceOperationPayload(
                credential_id=session.credential_id,
                elevator_id=body.elevator_id,
                operation_type=MaintenanceOperationType.ENTER_MAINTENANCE,
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )
    else:
        acs_res = await FleetAccessControl.validate_emergency_override(
            EmergencyOverridePayload(
                credential_id=session.credential_id,
                override_type=EmergencyOverrideType.MANUAL_RECALL,
                target_car_id=body.elevator_id,
                incident_id=None,
                reason=f"Stub state transition to {body.state}",
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )

    if not acs_res.allowed:
        return auth_result_response(acs_res)

    try:
        operational_simulator.isolate(
            body.elevator_id, body.state, technician_id=session.credential_id  # type: ignore[arg-type]
        )
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return operational_simulator.fleet_status()
