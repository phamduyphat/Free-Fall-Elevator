from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from acs.enums import DashboardOperationType, ElevatorId
from analytics.enums import TrafficProfile
from analytics.metrics import Metrics
from analytics.reports import (
    CarUtilizationReport,
    DowntimeReport,
    SecuritySummaryReport,
    TravelTimeReport,
    TripDistributionReport,
    WaitTimeReport,
)
from api.helpers import (
    auth_denial_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock

router = APIRouter(tags=["analytics-metrics"])


@router.get("/wait-times", response_model=WaitTimeReport)
async def get_wait_time_kpis(
    start_time: datetime,
    end_time: datetime,
    traffic_profile: Optional[TrafficProfile | str] = None,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> WaitTimeReport | JSONResponse:
    """Compute hall-call wait-time KPIs over `[start_time, end_time]` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_wait_time_kpis(
        start_time=start_time,
        end_time=end_time,
        traffic_profile=traffic_profile,
        db_pool=db_pool,
    )


@router.get("/travel-times", response_model=TravelTimeReport)
async def get_travel_time_kpis(
    start_time: datetime,
    end_time: datetime,
    elevator_id: Optional[ElevatorId | str] = None,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> TravelTimeReport | JSONResponse:
    """Compute cab physical travel-time KPIs over `[start_time, end_time]` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_travel_time_kpis(
        start_time=start_time,
        end_time=end_time,
        elevator_id=str(elevator_id) if elevator_id is not None else None,
        db_pool=db_pool,
    )


@router.get("/trip-distribution", response_model=TripDistributionReport)
async def get_trip_distribution(
    start_time: datetime,
    end_time: datetime,
    elevator_id: Optional[ElevatorId | str] = None,
    traffic_profile: Optional[TrafficProfile | str] = None,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> TripDistributionReport | JSONResponse:
    """Compute directional trip distribution shares over `[start_time, end_time]` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_trip_distribution(
        start_time=start_time,
        end_time=end_time,
        elevator_id=str(elevator_id) if elevator_id is not None else None,
        traffic_profile=traffic_profile,
        db_pool=db_pool,
    )


@router.get("/utilization", response_model=dict[str, CarUtilizationReport])
async def get_utilization_kpis(
    start_time: datetime,
    end_time: datetime,
    floor_height_meters: float = Query(default=3.0, gt=0),
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> dict[str, CarUtilizationReport] | JSONResponse:
    """Compute per-car distance, starts, and operational state hours (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_utilization_kpis(
        start_time=start_time,
        end_time=end_time,
        floor_height_meters=floor_height_meters,
        db_pool=db_pool,
    )


@router.get("/downtime", response_model=DowntimeReport)
async def get_downtime_summary(
    start_time: datetime,
    end_time: datetime,
    report_time: Optional[datetime] = None,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> DowntimeReport | JSONResponse:
    """Compute car downtime and SREMS availability over `[start_time, end_time]` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_downtime_summary(
        start_time=start_time,
        end_time=end_time,
        report_time=report_time or SimulationClock.now(),
        db_pool=db_pool,
    )


@router.get("/security", response_model=SecuritySummaryReport)
async def get_security_kpis(
    start_time: datetime,
    end_time: datetime,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> SecuritySummaryReport | JSONResponse:
    """Summarize denials, security alerts, and emergency incidents (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await Metrics.get_security_kpis(
        start_time=start_time,
        end_time=end_time,
        db_pool=db_pool,
    )

