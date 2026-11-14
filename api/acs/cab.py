from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from acs.cab_accesscontrol import CabAccessControl
from acs.enums import DenialReason, ElevatorId
from acs.payloads import (
    AuthorizationResult,
    CarDestinationRequestPayload,
    E5ServiceRunPayload,
)
from api.helpers import auth_result_response
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock
from simulation.operational import OperationalError, operational_simulator

router = APIRouter(prefix="/cab", tags=["acs-cab"])


class CabDestinationRequest(BaseModel):
    """In-cabin floor selection payload (`CabAccessControl.validate_car_destination`)."""

    elevator_id: ElevatorId | str
    origin_floor: int
    requested_floor: int


class E5ServiceRunRequest(BaseModel):
    """Dedicated E5 freight run payload (`CabAccessControl.validate_e5_service_run`)."""

    elevator_id: ElevatorId | str
    destination_floor: int
    purpose: str


@router.post("/destination", response_model=AuthorizationResult)
async def select_cab_destination(
    body: CabDestinationRequest,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Validate an in-cabin destination button press at `SimulationClock.now()`."""
    result = await CabAccessControl.validate_car_destination(
        CarDestinationRequestPayload(
            elevator_id=body.elevator_id,
            origin_floor=body.origin_floor,
            requested_floor=body.requested_floor,
            timestamp=SimulationClock.now(),
        ),
        db_pool=db_pool,
    )
    if result.allowed:
        try:
            trip = operational_simulator.submit_car_call(str(body.elevator_id), body.requested_floor)
            result.details["operational_request_id"] = trip["request_id"]
            result.details["operational_source"] = "labelled-test-stub"
        except OperationalError as exc:
            return auth_result_response(AuthorizationResult.deny(
                DenialReason.COMMAND_CONFLICT_REJECTED, message=str(exc)
            ))
    return auth_result_response(result)


@router.post("/e5/service-run", response_model=AuthorizationResult)
async def start_e5_service_run(
    body: E5ServiceRunRequest,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Validate a dedicated E5 freight/service run at `SimulationClock.now()`."""
    result = await CabAccessControl.validate_e5_service_run(
        E5ServiceRunPayload(
            elevator_id=body.elevator_id,
            destination_floor=body.destination_floor,
            purpose=body.purpose,
            timestamp=SimulationClock.now(),
        ),
        db_pool=db_pool,
    )
    if result.allowed:
        try:
            trip = operational_simulator.start_service_run(body.destination_floor)
            result.details["operational_request_id"] = trip["request_id"]
            result.details["operational_source"] = "labelled-test-stub"
        except OperationalError as exc:
            return auth_result_response(AuthorizationResult.deny(
                DenialReason.COMMAND_CONFLICT_REJECTED, message=str(exc)
            ))
    return auth_result_response(result)

