"""Mode 1 operational API backed by a clearly labelled, in-memory test stub."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from acs.enums import DashboardOperationType
from api.helpers import auth_denial_response, authorize_dashboard_op, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.operational import OperationalError, operational_simulator

router = APIRouter(prefix="/operational", tags=["operational-test-stub"])


class HallCallInput(BaseModel):
    origin_floor: int = Field(ge=-1, le=30)
    call_type: Literal["ORDINARY", "FREIGHT"] = "ORDINARY"
    direction: Literal["UP", "DOWN"] | None = None
    traffic_profile: str | None = None


class StubAdvanceInput(BaseModel):
    seconds: int = Field(default=1, ge=0, le=10000)


class StubStateInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]
    state: Literal["IN_SERVICE", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"]


@router.post("/hall-calls", status_code=201)
async def submit_hall_call(body: HallCallInput) -> dict:
    """Unauthenticated passenger hall call, with duplicate merging and E5 exceptions."""
    try:
        request, merged = operational_simulator.submit_hall_call(
            origin_floor=body.origin_floor,
            direction=body.direction,
            call_type=body.call_type,
            traffic_profile=getattr(body, "traffic_profile", None),
        )
        return {"request": request, "merged": merged, "source": "labelled-test-stub"}
    except OperationalError as exc:
        raise HTTPException(status_code=409 if "unavailable" in str(exc) else 422,
                            detail=str(exc)) from exc


@router.get("/hall-calls")
async def list_hall_calls() -> dict:
    return {"source": "labelled-test-stub", "requests": operational_simulator.hall_call_status()}


@router.get("/hall-calls/{request_id}")
async def get_hall_call(request_id: str) -> dict:
    try:
        return {"source": "labelled-test-stub", "request": operational_simulator.hall_call_status(request_id)}
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
    return operational_simulator.advance(body.seconds)


@router.post("/stub/state")
async def update_test_stub_car(
    body: StubStateInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Admin-only synthetic equipment-state event for testing dispatch exclusions.

    This endpoint does not implement physical interlocks, fault recovery or
    certified emergency behavior. It is deliberately labelled test-stub.
    """
    authorized, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(authorized)
    operational_simulator.isolate(body.elevator_id, body.state)
    return operational_simulator.fleet_status()
