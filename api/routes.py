from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from api.acs import router as acs_router
from api.analytics import router as analytics_router
from api.emulator.system import router as system_router

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    timestamp: datetime


class ElevatorStatus(BaseModel):
    elevator_id: str
    state: Literal["UNKNOWN"]
    current_floor: int | None
    direction: Literal["UP", "DOWN", "IDLE", "UNKNOWN"]
    available: bool | None
    message: str


class FleetResponse(BaseModel):
    elevators: list[ElevatorStatus]
    source: Literal["operational-state-unavailable"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="srems-api",
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/fleet/status", response_model=FleetResponse)
async def fleet_status() -> FleetResponse:
    return FleetResponse(
        elevators=[
            ElevatorStatus(
                elevator_id=elevator_id,
                state="UNKNOWN",
                current_floor=None,
                direction="UNKNOWN",
                available=None,
                message="Operational state source is not connected.",
            )
            for elevator_id in ("E1", "E2", "E3", "E4", "E5")
        ],
        source="operational-state-unavailable",
    )


router.include_router(system_router)
router.include_router(acs_router)
router.include_router(analytics_router)
