from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Body, APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from acs.cab_accesscontrol import CabAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import DashboardOperationType
from api.helpers import auth_denial_response, authorize_dashboard_op, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.operational import operational_simulator
from simulation.clock import (
    DEFAULT_BASE_TIME,
    DEFAULT_SIM_SECONDS_PER_TICK,
    DEFAULT_TICK_INTERVAL_SECONDS,
    ClockSnapshot,
    SimulationClock,
)

router = APIRouter()


class SystemResetRequest(BaseModel):
    """Parameters for resetting simulation databases, ACS state, and the virtual clock."""

    base_time: datetime = DEFAULT_BASE_TIME
    sim_seconds_per_tick: float = Field(default=DEFAULT_SIM_SECONDS_PER_TICK, gt=0)
    tick_interval_seconds: Optional[float] = Field(
        default=DEFAULT_TICK_INTERVAL_SECONDS, ge=0
    )
    start_loop: bool = True


class ManualTickRequest(BaseModel):
    """Optional step count for manual virtual clock advancement."""

    steps: int = Field(default=1, ge=0, le=100000)


class SystemResetResponse(BaseModel):
    """Response returned after a full database, session, and clock reset."""

    status: str
    clock: ClockSnapshot


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


@router.post("/system/reset", response_model=SystemResetResponse)
async def reset_system(
    body: SystemResetRequest = Body(default_factory=SystemResetRequest),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> SystemResetResponse | JSONResponse:
    """Reset PostgreSQL tables, ACS in-memory state, and `SimulationClock` (ADMIN only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    SimulationClock.stop()
    await db_pool.reset_database_tables()
    operational_simulator.reset()
    CabAccessControl.reset_in_memory_state()
    DashboardAccessControl.reset_in_memory_state()

    snapshot = SimulationClock.reset(
        base_time=body.base_time,
        sim_seconds_per_tick=body.sim_seconds_per_tick,
        tick_interval_seconds=body.tick_interval_seconds,
        start_loop=body.start_loop,
    )
    return SystemResetResponse(status="reset", clock=snapshot)


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

