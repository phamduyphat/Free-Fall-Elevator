from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from api.acs import router as acs_router
from api.analytics import router as analytics_router
from api.emulator.system import router as system_router
from api.operational import router as operational_router
from api.incident import router as incident_router
from simulation.operational import operational_simulator

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    timestamp: datetime


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok", service="srems-api", timestamp=datetime.now(timezone.utc)
    )


@router.get("/fleet/status")
async def fleet_status() -> dict:
    """Serve current positions from the *labelled software test stub* (not equipment)."""
    return operational_simulator.fleet_status()


router.include_router(system_router)
router.include_router(operational_router)
router.include_router(incident_router)
router.include_router(acs_router)
router.include_router(analytics_router)
