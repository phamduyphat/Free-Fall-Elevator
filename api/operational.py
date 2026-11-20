"""Mode 1 operational API backed by a clearly labelled, in-memory test stub."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acs.enums import (
    DashboardOperationType,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
)
from acs.fleet_accesscontrol import FleetAccessControl
from acs.helpers import reject_malformed_msg
from acs.payloads import (
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    ReturnToServicePayload,
)
from api.helpers import (
    auth_denial_response,
    auth_result_response,
    authorize_dashboard_op,
    extract_bearer_token,
)
from db import DbConnectionPool, get_db_pool
from simulation.clock import SimulationClock
from simulation.operational import OperationalError, operational_simulator
from simulation.wear_tear import WearTearConfig, run_wear_tear_benchmark
from simulation.traffic_profiles import inject_load_burst
from simulation.cibse_benchmark import (
    CIBSE_MORNING_PEAK,
    CIBSE_EVENING_PEAK,
    CIBSE_OFF_PEAK,
    CAP1_STRESS_BURST,
    run_cibse_benchmark,
)

router = APIRouter(prefix="/operational", tags=["operational-test-stub"])

_VALID_STUB_STATES = frozenset(
    {"IN_SERVICE", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"}
)


class HallCallInput(BaseModel):
    origin_floor: int
    call_type: str = "ORDINARY"
    direction: Optional[str] = None
    traffic_profile: Optional[str] = None


class StubAdvanceInput(BaseModel):
    seconds: int = 1


class StubStateInput(BaseModel):
    elevator_id: str
    state: str


@router.post("/hall-calls", status_code=201)
async def submit_hall_call(
    body: HallCallInput,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Unauthenticated passenger hall call, with duplicate merging, malformed-input audit, and E5 exceptions."""
    valid_floor = body.origin_floor == -1 or (1 <= body.origin_floor <= 30)
    valid_type = body.call_type in ("ORDINARY", "FREIGHT")
    valid_dir = (
        body.direction in ("UP", "DOWN")
        if body.call_type == "ORDINARY"
        else body.direction is None
    )
    if not (valid_floor and valid_type and valid_dir):
        denied = await reject_malformed_msg(
            timestamp=SimulationClock.now(),
            target_id="HALL_PANEL",
            source="HallCallInput",
            payload_details={
                "origin_floor": body.origin_floor,
                "call_type": body.call_type,
                "direction": body.direction,
            },
            message="Invalid hall-call floor, type, or direction.",
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    try:
        request, merged = operational_simulator.submit_hall_call(
            origin_floor=body.origin_floor,
            direction=body.direction,  # type: ignore[arg-type]
            call_type=body.call_type,  # type: ignore[arg-type]
            traffic_profile=body.traffic_profile,
        )
        return {"request": request, "merged": merged, "source": "labelled-test-stub"}
    except OperationalError as exc:
        raise HTTPException(
            status_code=409 if "unavailable" in str(exc) else 422,
            detail=str(exc),
        ) from exc


@router.get("/hall-calls")
async def list_hall_calls() -> dict:
    return {"source": "labelled-test-stub", "requests": operational_simulator.hall_call_status()}


@router.get("/hall-calls/{request_id}")
async def get_hall_call(request_id: str) -> dict:
    try:
        return {
            "source": "labelled-test-stub",
            "request": operational_simulator.hall_call_status(request_id),
        }
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
    if not (0 <= body.seconds <= 10000):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id="SIMULATOR",
            source="StubAdvanceInput",
            payload_details={"seconds": body.seconds},
            message="seconds must be an integer between 0 and 10000.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    return operational_simulator.advance(body.seconds)


@router.post("/stub/state")
async def update_test_stub_car(
    body: StubStateInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Admin-only synthetic equipment-state event routed through FleetAccessControl and audit_log_chain."""
    authorized, session = await authorize_dashboard_op(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool
    )
    if session is None:
        return auth_denial_response(authorized)

    if not ElevatorId.has_value(body.elevator_id) or body.state not in _VALID_STUB_STATES:
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="StubStateInput",
            payload_details={"elevator_id": body.elevator_id, "state": body.state},
            message="Invalid elevator_id or operational state.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    if body.state == "IN_SERVICE":
        if not operational_simulator.can_return_to_service(body.elevator_id):
            raise HTTPException(
                status_code=409,
                detail="Recovery checks failed: stale, fault, door, or unresolved incident.",
            )
        acs_res = await FleetAccessControl.validate_return_to_service(
            ReturnToServicePayload(
                credential_id=session.credential_id,
                elevator_id=body.elevator_id,
                technician_confirmation=True,
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )
    elif body.state == "MAINTENANCE":
        acs_res = await FleetAccessControl.validate_maintenance_operation(
            MaintenanceOperationPayload(
                credential_id=session.credential_id,
                elevator_id=body.elevator_id,
                operation_type=MaintenanceOperationType.ENTER_MAINTENANCE,
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )
    else:
        acs_res = await FleetAccessControl.validate_emergency_override(
            EmergencyOverridePayload(
                credential_id=session.credential_id,
                override_type=EmergencyOverrideType.MANUAL_RECALL,
                target_car_id=body.elevator_id,
                incident_id=None,
                reason=f"Stub state transition to {body.state}",
                timestamp=session.timestamp,
            ),
            db_pool=db_pool,
        )

    if not acs_res.allowed:
        return auth_result_response(acs_res)

    try:
        operational_simulator.isolate(
            body.elevator_id, body.state, technician_id=session.credential_id  # type: ignore[arg-type]
        )
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return operational_simulator.fleet_status()


class WearTearConfigInput(BaseModel):
    enabled: Optional[bool] = None
    strategy: Optional[str] = None
    monthly_target_pct: Optional[float] = None
    monthly_ceiling_pct: Optional[float] = None
    six_month_target_pct: Optional[float] = None
    six_month_ceiling_pct: Optional[float] = None
    safety_margin_pct: Optional[float] = None
    off_peak_start_hour: Optional[int] = None
    off_peak_end_hour: Optional[int] = None


class WearTearResetInput(BaseModel):
    scope: str = "ALL"


class WearTearBenchmarkInput(BaseModel):
    seed: int = 42
    months: int = 6
    days_per_month: int = 30
    calls_per_day: int = 240
    strategy: Optional[str] = None
    enabled: Optional[bool] = None
    compare_baseline: bool = True


@router.get("/wear-tear/config")
async def get_wear_tear_config() -> dict:
    """Retrieve active wear-and-tear balancing configuration."""
    return operational_simulator.get_wear_tear_config()


@router.post("/wear-tear/config")
async def update_wear_tear_config(body: WearTearConfigInput) -> dict:
    """Update wear-and-tear configuration (toggle, strategy, thresholds)."""
    current = operational_simulator.get_wear_tear_config()
    patch = body.model_dump(exclude_none=True)
    current.update(patch)
    return operational_simulator.update_wear_tear_config(current)


@router.get("/wear-tear/status")
async def get_wear_tear_status() -> dict:
    """Retrieve current wear-and-tear counters, spread %, and compliance metrics."""
    return operational_simulator.get_wear_tear_status()


@router.post("/wear-tear/reset")
async def reset_wear_tear_counters(body: WearTearResetInput) -> dict:
    """Reset wear-and-tear counters for specified scope (MONTHLY, SIX_MONTHS, ALL)."""
    return operational_simulator.reset_wear_tear_counters(body.scope)


@router.post("/wear-tear/benchmark")
async def run_wear_tear_benchmark_api(body: WearTearBenchmarkInput) -> dict:
    """Execute reproducible 6-month simulated load benchmark and return graph dataset."""
    cfg_data = operational_simulator.get_wear_tear_config()
    if body.strategy:
        cfg_data["strategy"] = body.strategy
    if body.enabled is not None:
        cfg_data["enabled"] = body.enabled
    cfg = WearTearConfig.from_dict(cfg_data)

    res = run_wear_tear_benchmark(
        seed=body.seed,
        months=max(1, min(12, body.months)),
        days_per_month=max(1, min(31, body.days_per_month)),
        calls_per_day=max(10, min(1000, body.calls_per_day)),
        config=cfg,
        compare_baseline=body.compare_baseline,
    )
    return res.to_dict()


class LoadBurstInput(BaseModel):
    count: int = 50
    profile: str = "RANDOM"
    auto_step_seconds: int = 0
    seed: Optional[int] = None


class LiveBenchmarkInput(BaseModel):
    scenario: str = "MORNING_PEAK"
    reset_sim: bool = False
    seed: Optional[int] = None
    wear_tear_enabled: Optional[bool] = None
    strategy: Optional[str] = None


@router.post("/load/burst")
async def inject_traffic_burst(body: LoadBurstInput) -> dict:
    """Inject a high-volume load burst of simulated passenger calls into the live simulator."""
    result = inject_load_burst(
        sim=operational_simulator,
        count=body.count,
        profile=body.profile,
        auto_step_seconds=body.auto_step_seconds,
        seed=body.seed,
    )
    result["injected_count"] = result["submitted"]
    result["active_calls_count"] = result["active_hall_calls"]
    result["fleet_status"] = operational_simulator.fleet_status()
    result["metrics"] = operational_simulator.metrics()
    return result


@router.post("/benchmark/live")
async def run_live_simulation_benchmark(body: LiveBenchmarkInput) -> dict:
    """Execute a CIBSE Guide D benchmark scenario directly on the active simulation."""
    scenarios = {
        "MORNING_PEAK": CIBSE_MORNING_PEAK,
        "EVENING_PEAK": CIBSE_EVENING_PEAK,
        "OFF_PEAK": CIBSE_OFF_PEAK,
        "STRESS": CAP1_STRESS_BURST,
    }
    selected_scenario = scenarios.get(body.scenario.upper(), CIBSE_MORNING_PEAK)
    if body.seed is not None:
        from dataclasses import replace
        selected_scenario = replace(selected_scenario, seed=body.seed)

    if body.wear_tear_enabled is not None:
        operational_simulator.wear_tear_config.enabled = body.wear_tear_enabled
    if body.strategy is not None:
        operational_simulator.wear_tear_config.strategy = body.strategy  # type: ignore[assignment]

    metrics = run_cibse_benchmark(
        sim=operational_simulator,
        scenario=selected_scenario,
        reset_sim=body.reset_sim,
        wear_tear_enabled=body.wear_tear_enabled,
        wear_tear_strategy=body.strategy,
    )
    m_dict = metrics.to_dict()
    m_dict["scenario"] = metrics.scenario_name
    m_dict["total_calls"] = metrics.total_calls_submitted
    m_dict["mean_wait_time"] = metrics.mean_wait_seconds
    m_dict["median_wait_time"] = metrics.median_wait_seconds
    m_dict["p90_wait_time"] = metrics.p90_wait_seconds
    m_dict["p95_wait_time"] = metrics.p95_wait_seconds
    m_dict["max_wait_time"] = metrics.max_wait_seconds
    m_dict["mean_transit_time"] = metrics.mean_travel_seconds
    m_dict["mean_journey_time"] = round(metrics.mean_wait_seconds + metrics.mean_travel_seconds, 2)
    m_dict["max_hall_queue_depth"] = metrics.peak_simultaneous_active_queue
    m_dict["awt_target_met"] = metrics.target_met
    m_dict["p95_target_met"] = metrics.p95_wait_seconds <= metrics.target_p95_seconds
    m_dict["queue_target_met"] = metrics.queue_bounded_pass
    m_dict["wear_tear_enabled"] = metrics.wear_tear_enabled
    m_dict["wear_tear_strategy"] = metrics.wear_tear_strategy
    m_dict["wear_spread_pct"] = metrics.wear_spread_pct
    m_dict["wear_target_met"] = metrics.wear_target_met

    return {
        "metrics": m_dict,
        "summary_text": metrics.print_summary(),
        "fleet_status": operational_simulator.fleet_status(),
        "trips": metrics.trips,
    }


