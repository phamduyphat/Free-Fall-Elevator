"""Mode 3/4 test-stub actions. Staff controls use the existing ACS dashboard session.

This module never issues safety-critical hardware commands. Synthetic events and
operation results must not be presented as equipment-derived telemetry.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from acs.enums import DashboardOperationType
from api.helpers import auth_denial_response, authorize_dashboard_op, extract_bearer_token
from db import DbConnectionPool, get_db_pool
from simulation.operational import OperationalError, operational_simulator

router = APIRouter(prefix="/operational", tags=["operational-safety-test-stub"])


class HelpInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]
    option: Literal["HELP", "MEDICAL"]


class DoorInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]
    action: Literal["OPEN", "CLOSE"]


class IncidentInput(BaseModel):
    incident_id: str
    action: Literal["ACKNOWLEDGE", "RESOLVE", "CLOSE"]
    recovery_confirmed: bool = False


class IdInput(BaseModel):
    incident_id: str


class CarInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]


class PhoneMedicalInput(BaseModel):
    floor: int = Field(ge=1, le=30)


class RescueInput(BaseModel):
    incident_id: str
    wait_seconds: int = Field(default=120, ge=1, le=600)
    confirmed_empty: bool = False


class BuildingInput(BaseModel):
    kind: Literal["FIRE", "POWER_FAILURE"]


class FaultInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]
    fault_code: str = Field(min_length=1, max_length=80)


class ObstructionInput(BaseModel):
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"]
    active: bool


def _perform(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _staff(op: DashboardOperationType, token: str, pool: type[DbConnectionPool],
                 *, admin_only: bool = False):
    result, session = await authorize_dashboard_op(op, token, pool)
    if session is None:
        return None, auth_denial_response(result)
    if admin_only and session.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only an Admin may perform this operation")
    return session, None


@router.post("/emergency/trigger", status_code=201)
async def trigger_emergency(body: HelpInput) -> dict:
    """Passenger HELP does not isolate a car; MEDICAL does (EMR-3.1/3.14)."""
    result = _perform(operational_simulator.trigger_emergency, body.elevator_id, body.option)
    return {"source": "labelled-test-stub", "incident": result}


@router.post("/doors")
async def passenger_door_command(body: DoorInput) -> dict:
    return _perform(operational_simulator.passenger_door_command, body.elevator_id, body.action)


@router.get("/summary")
async def operational_summary() -> dict:
    """Public *aggregate only* demo indicators. No credential or audit detail."""
    return operational_simulator.metrics()


@router.get("/incidents")
async def list_incidents(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "items": operational_simulator.list_incidents()}


@router.get("/events")
async def list_events(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.VIEW_AUDIT_LOGS, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "integrity_verified": False,
            "notice": "Process-local events are NOT the tamper-evident ACS audit chain",
            "items": operational_simulator.recent_events()}


@router.post("/incidents/action")
async def act_on_incident(
    body: IncidentInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    incident = next((i for i in operational_simulator.list_incidents()
                     if i["incident_id"] == body.incident_id), None)
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident ID")
    # Only Admins resolve medical, fire, door obstruction, and help alerts.
    admin_required = incident["kind"] in {"MEDICAL", "FIRE", "DOOR_OBSTRUCTION", "HELP", "REPEATED_EMERGENCY"}
    if body.action in ("RESOLVE", "CLOSE") and admin_required and session.role != "ADMIN":
        raise HTTPException(status_code=403, detail="This incident requires an Admin resolver")
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.incident_action, body.incident_id, body.action,
        actor=session.credential_id, technician_confirmation=body.recovery_confirmed)}


@router.post("/incidents/confirm-stuck")
async def confirm_stuck(
    body: IdInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.confirm_help_as_stuck, body.incident_id)}


@router.post("/incidents/phone-medical")
async def create_phone_medical(
    body: PhoneMedicalInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.phone_medical_incident, body.floor)}


@router.post("/incidents/rescue")
async def send_rescue_car(
    body: RescueInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", **_perform(
        operational_simulator.dispatch_rescue, body.incident_id,
        wait_seconds=body.wait_seconds, confirmed_empty=body.confirmed_empty)}


@router.post("/incidents/confirm-boarding")
async def confirm_rescue_boarding(
    body: IdInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.confirm_rescue_boarding, body.incident_id)}


@router.post("/stub/building-event")
async def inject_building_event(
    body: BuildingInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.building_emergency, body.kind)}


@router.post("/stub/fault")
async def inject_fault(
    body: FaultInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.report_fault, body.elevator_id, body.fault_code)}


@router.post("/stub/fault-clear")
async def clear_fault_event(
    body: CarInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return _perform(operational_simulator.clear_fault, body.elevator_id)


@router.post("/stub/obstruction")
async def inject_obstruction(
    body: ObstructionInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return _perform(operational_simulator.door_obstruction, body.elevator_id, body.active)


@router.post("/stub/communication-loss")
async def inject_communication_loss(
    body: CarInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "incident": _perform(
        operational_simulator.communication_loss, body.elevator_id)}


@router.post("/stub/reconcile")
async def reconcile_after_connection_loss(
    body: CarInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return _perform(operational_simulator.reconcile_connection, body.elevator_id)

class BoardingEvidenceInput(BaseModel):
    request_id: str
    boarded: bool


class HallCancelInput(BaseModel):
    request_id: str


@router.post("/stub/boarding-evidence")
async def inject_boarding_evidence(
    body: BoardingEvidenceInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "request": _perform(
        operational_simulator.boarding_evidence, body.request_id, body.boarded)}


@router.post("/hall-calls/cancel")
async def admin_cancel_hall_call(
    body: HallCancelInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool,
                                  admin_only=True)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "request": _perform(
        operational_simulator.cancel_hall_call, body.request_id, session.credential_id)}


@router.get("/diagnostics/{elevator_id}")
async def view_diagnostics(
    elevator_id: Literal["E1", "E2", "E3", "E4", "E5"],
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool)
    if error is not None:
        return error
    return _perform(operational_simulator.diagnostics, elevator_id)
