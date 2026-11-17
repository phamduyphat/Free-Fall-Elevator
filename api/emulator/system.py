from __future__ import annotations

from fastapi import Body, APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from acs.enums import DashboardOperationType
from api.helpers import auth_denial_response, authorize_dashboard_op, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.clock import (
    ClockSnapshot,
    SimulationClock,
)

router = APIRouter()


class ManualTickRequest(BaseModel):
    """Optional step count for manual virtual clock advancement."""

    steps: int = Field(default=1, ge=0, le=100000)


@router.get("/health/db")
async def health_db() -> JSONResponse:
    """Verify PostgreSQL pool initialization and connectivity (`SELECT 1`)."""
    try:
        async with DbConnectionPool.acquire() as conn:
            val = await conn.fetchval("SELECT 1;")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "database": "connected", "result": val},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "database": "disconnected",
                "detail": str(exc),
            },
        )


@router.get("/system/clock", response_model=ClockSnapshot)
async def get_simulation_clock() -> ClockSnapshot:
    """Inspect the current virtual simulation clock state (`T_sim`, `T_0`, `tick`, `dt`)."""
    return SimulationClock.snapshot()


@router.post("/system/clock/tick", response_model=ClockSnapshot)
async def advance_simulation_clock(
    body: ManualTickRequest = Body(default_factory=ManualTickRequest),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> ClockSnapshot | JSONResponse:
    """Manually advance the virtual clock by `steps` ticks (ADMIN only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return SimulationClock.advance(steps=body.steps)

