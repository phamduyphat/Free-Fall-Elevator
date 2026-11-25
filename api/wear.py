"""Wear-and-tear (trip counter) API — instructor NFR WEAR-1..WEAR-6.

* ``GET  /api/v1/operational/wear``            live counters + monthly / 6-month compliance
                                                (Admin or Technician, VIEW_FLEET_STATUS).
* ``POST /api/v1/operational/wear/benchmark``  start an accelerated 1..6 month simulation on a
                                                *separate* simulator instance (Admin only).
* ``GET  /api/v1/operational/wear/benchmark``  progress and results of that run (Admin only).
* ``GET  /api/v1/operational/wear/report``     pre-computed 6-month report shipped in
                                                ``docs/wear_report.json`` (Admin only).

The accelerated run never touches the live ``operational_simulator`` and writes nothing to
PostgreSQL; its results are labelled as test-stub output (SWI-2).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acs.enums import DashboardOperationType
from api.helpers import auth_denial_response, authorize_dashboard_op, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.operational import operational_simulator
from simulation.wear_benchmark import WearBenchmarkConfig, default_outages, run_wear_benchmark

router = APIRouter(prefix="/operational/wear", tags=["wear-and-tear"])

REPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "wear_report.json"


class BenchmarkInput(BaseModel):
    months: int = 1
    compare: bool = True
    outages: bool = True
    seed: int = 2030
    start: Optional[str] = None


class _Job:
    """Single background benchmark job (one at a time keeps the server responsive)."""

    lock = threading.Lock()
    state: dict[str, Any] = {"status": "IDLE"}


async def _authorize(op: DashboardOperationType, token: str, pool: type[DbConnectionPool]):
    result, session = await authorize_dashboard_op(op, token, pool)
    if session is None:
        return None, auth_denial_response(result)
    return session, None


@router.get("")
async def wear_status(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    _, error = await _authorize(DashboardOperationType.VIEW_FLEET_STATUS, session_token, db_pool)
    if error is not None:
        return error
    return operational_simulator.wear_status()


def _run_job(body: BenchmarkInput, started_by: str) -> None:
    start = (datetime.fromisoformat(body.start).replace(tzinfo=timezone.utc)
             if body.start else datetime(2026, 1, 1, tzinfo=timezone.utc))
    outages = default_outages(start, body.months, body.seed) if body.outages else []
    runs = [True, False] if body.compare else [True]
    results = []
    try:
        for i, enabled in enumerate(runs):
            label = "wear-balancing ON" if enabled else "baseline (balancing OFF)"

            def progress(frac: float, msg: str, i: int = i) -> None:
                with _Job.lock:
                    _Job.state["progress"] = round((i + frac) / len(runs), 4)
                    _Job.state["message"] = msg

            cfg = WearBenchmarkConfig(months=body.months, start=start, seed=body.seed,
                                      balancing_enabled=enabled, outages=outages)
            results.append(run_wear_benchmark(cfg, label=label, progress=progress).to_dict())
        with _Job.lock:
            _Job.state.update(status="DONE", progress=1.0, message="Finished", results=results,
                              finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # pragma: no cover - surfaced to the dashboard
        with _Job.lock:
            _Job.state.update(status="FAILED", message=str(exc))


@router.post("/benchmark", status_code=202)
async def start_benchmark(
    body: BenchmarkInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _authorize(DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool)
    if error is not None:
        return error
    if not 1 <= body.months <= 6:
        raise HTTPException(status_code=422, detail="months must be between 1 and 6")
    if body.start:
        try:
            datetime.fromisoformat(body.start)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="start must be an ISO date") from exc
    with _Job.lock:
        if _Job.state.get("status") == "RUNNING":
            raise HTTPException(status_code=409, detail="A wear benchmark is already running")
        _Job.state = {
            "status": "RUNNING",
            "progress": 0.0,
            "message": "Starting",
            "params": body.model_dump(),
            "started_by": session.credential_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source": "labelled-test-stub (separate simulator instance; live fleet untouched)",
        }
    threading.Thread(target=_run_job, args=(body, session.credential_id), daemon=True).start()
    return _Job.state


@router.get("/benchmark")
async def benchmark_status(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    _, error = await _authorize(DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool)
    if error is not None:
        return error
    with _Job.lock:
        return dict(_Job.state)


@router.get("/report")
async def precomputed_report(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    _, error = await _authorize(DashboardOperationType.VIEW_ANALYTICS, session_token, db_pool)
    if error is not None:
        return error
    if not REPORT_PATH.exists():
        raise HTTPException(status_code=404, detail="docs/wear_report.json has not been generated")
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
