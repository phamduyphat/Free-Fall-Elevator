from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from acs.cab_accesscontrol import CabAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import DenialReason, ElevatorId
from acs.payloads import AuthorizationResult, CardSwipePayload
from api.helpers import auth_denial_response, auth_result_response, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock

router = APIRouter(prefix="/auth", tags=["acs-auth"])


class CabSwipeRequest(BaseModel):
    """In-cabin card presentation payload (`CabAccessControl.validate_card_swipe`)."""

    elevator_id: Optional[ElevatorId | str] = None
    credential_id: str
    origin_floor: int = 1


class DashboardLoginRequest(BaseModel):
    """Dashboard login card swipe payload (`DashboardAccessControl.validate_card_swipe`)."""

    credential_id: str


@router.post("/cab/swipe", response_model=AuthorizationResult)
async def cab_card_swipe(
    body: CabSwipeRequest,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Validate an in-cabin card swipe at `SimulationClock.now()`."""
    result = await CabAccessControl.validate_card_swipe(
        CardSwipePayload(
            elevator_id=body.elevator_id,
            credential_id=body.credential_id,
            origin_floor=body.origin_floor,
            is_dashboard_access=False,
            timestamp=SimulationClock.now(),
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.post("/dashboard/login", response_model=AuthorizationResult)
async def dashboard_login(
    body: DashboardLoginRequest,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Authenticate an Admin or Technician credential and issue a 15-minute dashboard token."""
    result = await DashboardAccessControl.validate_card_swipe(
        CardSwipePayload(
            credential_id=body.credential_id,
            is_dashboard_access=True,
            timestamp=SimulationClock.now(),
        ),
        db_pool=db_pool,
    )
    return auth_result_response(result)


@router.post("/dashboard/logout", response_model=AuthorizationResult)
async def dashboard_logout(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> AuthorizationResult | JSONResponse:
    """Terminate the active dashboard session identified by the Bearer token."""
    if not session_token:
        return auth_denial_response(
            AuthorizationResult.deny(
                DenialReason.SESSION_INVALID,
                message="Authorization Bearer session token is required.",
            )
        )

    result = await DashboardAccessControl.logout_dashboard_session(
        session_token,
        timestamp=SimulationClock.now(),
        db_pool=db_pool,
    )
    return auth_result_response(result)

