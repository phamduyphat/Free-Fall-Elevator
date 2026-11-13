from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from acs.enums import DashboardOperationType
from analytics.export_engine import ExportEngine
from analytics.reports import ExportPayload
from api.helpers import (
    auth_denial_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool

router = APIRouter(tags=["analytics-export"])


class AnalyticsExportRequest(BaseModel):
    """Export trigger contract (`ExportEngine.export_run_report`)."""

    run_id: str
    export_format: Literal["JSON", "CSV"] = "JSON"
    include_per_request: bool = False
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@router.post("/export", response_model=ExportPayload)
async def export_analytics_report(
    body: AnalyticsExportRequest,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> ExportPayload | JSONResponse:
    """Export JSON or CSV summary/per-request metrics at `SimulationClock.now()` (`ADMIN` only)."""
    auth_res, session = await authorize_dashboard_op(
        DashboardOperationType.EXPORT_ANALYTICS_REPORTS, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(auth_res)

    return await ExportEngine.export_run_report(
        run_id=body.run_id,
        export_format=body.export_format,
        include_per_request=body.include_per_request,
        start_time=body.start_time,
        end_time=body.end_time,
        admin_id=session.credential_id,
        admin_role=session.role,
        current_time=session.timestamp,
        db_pool=db_pool,
    )

