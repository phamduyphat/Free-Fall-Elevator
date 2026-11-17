"""Mode 3/4 test-stub actions wired directly through FleetAccessControl and audit_log_chain.

This module never issues safety-critical hardware commands. Synthetic events and
operation results must not be presented as equipment-derived telemetry.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acs.enums import (
    DashboardOperationType,
    ElevatorId,
    EmergencyOverrideType,
)
from acs.fleet_accesscontrol import FleetAccessControl
from acs.helpers import reject_malformed_msg
from acs.payloads import (
    EmergencyOverridePayload,
    RescueCarDispatchPayload,
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

router = APIRouter(prefix="/operational", tags=["operational-safety-test-stub"])


class HelpInput(BaseModel):
    elevator_id: str
    option: str


class DoorInput(BaseModel):
    elevator_id: str
    action: str


class IncidentInput(BaseModel):
    incident_id: str
    action: str
    recovery_confirmed: bool = False


class IdInput(BaseModel):
    incident_id: str
    reason: str = "Admin emergency override"


class CarInput(BaseModel):
    elevator_id: str


class PhoneMedicalInput(BaseModel):
    floor: int


class RescueInput(BaseModel):
    incident_id: str
    wait_seconds: int = 120
    confirmed_empty: bool = False
    rescue_car_id: Optional[str] = None


class BuildingInput(BaseModel):
    kind: str


class FaultInput(BaseModel):
    elevator_id: str
    fault_code: str


class ObstructionInput(BaseModel):
    elevator_id: str
    active: bool


class BoardingEvidenceInput(BaseModel):
    request_id: str
    boarded: bool


class HallCancelInput(BaseModel):
    request_id: str
    reason: str = "ADMIN_CANCELLED"


def _perform(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _staff(
    op: DashboardOperationType,
    token: str,
    pool: type[DbConnectionPool],
    *,
    admin_only: bool = False,
):
    result, session = await authorize_dashboard_op(op, token, pool)
    if session is None:
        return None, auth_denial_response(result)
    if admin_only and session.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Only an Admin may perform this operation")
    return session, None


@router.post("/emergency/trigger", status_code=201)
async def trigger_emergency(
    body: HelpInput,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    """Passenger HELP does not isolate a car; MEDICAL does (EMR-3.1/3.14)."""
    if not ElevatorId.has_value(body.elevator_id) or body.option not in ("HELP", "MEDICAL"):
        denied = await reject_malformed_msg(
            timestamp=SimulationClock.now(),
            target_id=str(body.elevator_id),
            source="HelpInput",
            payload_details={"elevator_id": body.elevator_id, "option": body.option},
            message="Invalid elevator_id or emergency trigger option.",
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    result = _perform(operational_simulator.trigger_emergency, body.elevator_id, body.option)
    return {"source": "labelled-test-stub", "incident": result}


@router.post("/doors")
async def passenger_door_command(
    body: DoorInput,
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    if not ElevatorId.has_value(body.elevator_id) or body.action not in ("OPEN", "CLOSE"):
        denied = await reject_malformed_msg(
            timestamp=SimulationClock.now(),
            target_id=str(body.elevator_id),
            source="DoorInput",
            payload_details={"elevator_id": body.elevator_id, "action": body.action},
            message="Invalid elevator_id or door action.",
            db_pool=db_pool,
        )
        return auth_result_response(denied)
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
    _, error = await _staff(DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool)
    if error is not None:
        return error
    return {"source": "labelled-test-stub", "items": operational_simulator.list_incidents()}


@router.get("/events")
async def list_events(
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    _, error = await _staff(DashboardOperationType.VIEW_AUDIT_LOGS, session_token, db_pool)
    if error is not None:
        return error
    return {
        "source": "labelled-test-stub",
        "integrity_verified": False,
        "notice": "Process-local events are NOT the tamper-evident ACS audit chain",
        "items": operational_simulator.recent_events(),
    }


@router.post("/incidents/action")
async def act_on_incident(
    body: IncidentInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    if body.action not in ("ACKNOWLEDGE", "RESOLVE", "CLOSE", "FALSE_ALARM"):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=body.incident_id or "UNKNOWN",
            source="IncidentInput",
            payload_details={"incident_id": body.incident_id, "action": body.action},
            message=f"Invalid incident action '{body.action}'.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    incident = next(
        (i for i in operational_simulator.list_incidents() if i["incident_id"] == body.incident_id),
        None,
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident ID")

    def _preview_returning_cars() -> list[str]:
        _, returning_cars = operational_simulator.preview_incident_action(
            body.incident_id,
            body.action,
            technician_confirmation=body.recovery_confirmed,
        )
        return returning_cars

    try:
        acs_res = await FleetAccessControl.validate_incident_action(
            credential_id=session.credential_id,
            incident_id=body.incident_id,
            incident_kind=incident["kind"],
            elevator_id=incident["elevator_id"],
            action=body.action,
            recovery_confirmed=body.recovery_confirmed,
            precheck=_preview_returning_cars,
            timestamp=session.timestamp,
            db_pool=db_pool,
        )
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.incident_action,
            body.incident_id,
            body.action,
            actor=session.credential_id,
            technician_confirmation=body.recovery_confirmed,
        ),
    }


@router.post("/incidents/confirm-stuck")
async def confirm_stuck(
    body: IdInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error

    incident = next(
        (i for i in operational_simulator.list_incidents() if i["incident_id"] == body.incident_id),
        None,
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident ID")
    if incident["kind"] != "HELP" or incident["status"] in ("RESOLVED", "CLOSED", "FALSE_ALARM"):
        raise HTTPException(status_code=409, detail="Only an open HELP request can be confirmed as stuck")

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.CONFIRM_STUCK,
            target_car_id=incident["elevator_id"],
            incident_id=body.incident_id,
            reason=body.reason,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.confirm_help_as_stuck,
            body.incident_id,
            actor=session.credential_id,
        ),
    }


@router.post("/incidents/phone-medical")
async def create_phone_medical(
    body: PhoneMedicalInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error
    if not (1 <= body.floor <= 30):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id="DASHBOARD",
            source="PhoneMedicalInput",
            payload_details={"floor": body.floor},
            message="Medical rescue floor must be between 1 and 30.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.PHONE_MEDICAL,
            target_car_id=None,
            incident_id=None,
            reason=f"Telephone medical emergency reported at floor {body.floor}",
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(operational_simulator.phone_medical_incident, body.floor),
    }


@router.post("/incidents/rescue")
async def send_rescue_car(
    body: RescueInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error

    if body.rescue_car_id is not None and not ElevatorId.has_value(body.rescue_car_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.rescue_car_id),
            source="RescueInput",
            payload_details={"incident_id": body.incident_id, "rescue_car_id": body.rescue_car_id},
            message=f"Invalid rescue_car_id '{body.rescue_car_id}'.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    if not (1 <= body.wait_seconds <= 600):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.rescue_car_id or body.incident_id),
            source="RescueInput",
            payload_details={"incident_id": body.incident_id, "wait_seconds": body.wait_seconds},
            message="Rescue wait must be 1..600 seconds.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    try:
        chosen_car_id, incident_floor = operational_simulator.select_rescue_candidate(
            body.incident_id,
            wait_seconds=body.wait_seconds,
            confirmed_empty=body.confirmed_empty,
            preferred_car_id=body.rescue_car_id,
        )
    except OperationalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    acs_res = await FleetAccessControl.validate_rescue_car_dispatch(
        RescueCarDispatchPayload(
            credential_id=session.credential_id,
            incident_id=body.incident_id,
            rescue_car_id=chosen_car_id,
            incident_floor=incident_floor,
            dwell_wait_seconds=body.wait_seconds,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        **_perform(
            operational_simulator.dispatch_rescue,
            body.incident_id,
            wait_seconds=body.wait_seconds,
            confirmed_empty=body.confirmed_empty,
            rescue_car_id=chosen_car_id,
        ),
    }


@router.post("/incidents/rescue/cancel")
async def cancel_rescue_car(
    body: IdInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error

    incident = next(
        (i for i in operational_simulator.list_incidents() if i["incident_id"] == body.incident_id),
        None,
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident ID")
    if not incident.get("rescue_car_id"):
        raise HTTPException(status_code=409, detail="Incident has no active rescue car")

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.RESCUE_RELEASE,
            target_car_id=incident["rescue_car_id"],
            incident_id=body.incident_id,
            reason=body.reason,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.cancel_rescue,
            body.incident_id,
            actor=session.credential_id,
        ),
    }


@router.post("/incidents/confirm-boarding")
async def confirm_rescue_boarding(
    body: IdInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error

    incident = next(
        (i for i in operational_simulator.list_incidents() if i["incident_id"] == body.incident_id),
        None,
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Unknown incident ID")
    rescue_car_id = incident.get("rescue_car_id")
    if incident["kind"] != "MEDICAL" or not rescue_car_id:
        raise HTTPException(status_code=409, detail="Only a medical rescue can confirm boarding")
    car = operational_simulator.cars.get(rescue_car_id)
    if car is None or not car.rescue_active or car.stops or car.door_state != "OPEN":
        raise HTTPException(status_code=409, detail="Rescue car must be waiting with doors open")

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.RESCUE_BOARDING,
            target_car_id=rescue_car_id,
            incident_id=body.incident_id,
            reason="Admin confirmed ill person aboard rescue car",
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(operational_simulator.confirm_rescue_boarding, body.incident_id),
    }


@router.post("/stub/building-event")
async def inject_building_event(
    body: BuildingInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error
    if body.kind not in ("FIRE", "POWER_FAILURE"):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id="BUILDING",
            source="BuildingInput",
            payload_details={"kind": body.kind},
            message="Building event must be FIRE or POWER_FAILURE.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.BUILDING_EMERGENCY,
            target_car_id=None,
            incident_id=None,
            reason=f"Building-wide emergency triggered: {body.kind}",
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.building_emergency,
            body.kind,
            actor=session.credential_id,
        ),
    }


@router.post("/stub/fault")
async def inject_fault(
    body: FaultInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    if (
        not ElevatorId.has_value(body.elevator_id)
        or not body.fault_code.strip()
        or len(body.fault_code) > 80
    ):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="FaultInput",
            payload_details={"elevator_id": body.elevator_id, "fault_code": body.fault_code},
            message="Invalid elevator_id or fault_code.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.report_fault,
            body.elevator_id,
            body.fault_code,
            actor=session.credential_id,
        ),
    }


@router.post("/stub/fault-clear")
async def clear_fault_event(
    body: CarInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    if not ElevatorId.has_value(body.elevator_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="CarInput",
            payload_details={"elevator_id": body.elevator_id},
            message="Invalid elevator_id.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
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
    if not ElevatorId.has_value(body.elevator_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="ObstructionInput",
            payload_details={"elevator_id": body.elevator_id, "active": body.active},
            message="Invalid elevator_id.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
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
    if not ElevatorId.has_value(body.elevator_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="CarInput",
            payload_details={"elevator_id": body.elevator_id},
            message="Invalid elevator_id.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    return {
        "source": "labelled-test-stub",
        "incident": _perform(
            operational_simulator.communication_loss,
            body.elevator_id,
            actor=session.credential_id,
        ),
    }


@router.post("/stub/reconcile")
async def reconcile_after_connection_loss(
    body: CarInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    if not ElevatorId.has_value(body.elevator_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(body.elevator_id),
            source="CarInput",
            payload_details={"elevator_id": body.elevator_id},
            message="Invalid elevator_id.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)

    _perform(operational_simulator.can_reconcile_connection, body.elevator_id)
    acs_res = await FleetAccessControl.validate_return_to_service(
        ReturnToServicePayload(
            credential_id=session.credential_id,
            elevator_id=body.elevator_id,
            technician_confirmation=True,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return _perform(
        operational_simulator.reconcile_connection,
        body.elevator_id,
        actor=session.credential_id,
    )


@router.post("/stub/boarding-evidence")
async def inject_boarding_evidence(
    body: BoardingEvidenceInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    _, error = await _staff(DashboardOperationType.MAINTENANCE_CONTROL, session_token, db_pool)
    if error is not None:
        return error
    return {
        "source": "labelled-test-stub",
        "request": _perform(
            operational_simulator.boarding_evidence, body.request_id, body.boarded
        ),
    }


@router.post("/hall-calls/cancel")
async def admin_cancel_hall_call(
    body: HallCancelInput,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(
        DashboardOperationType.EMERGENCY_OVERRIDE, session_token, db_pool, admin_only=True
    )
    if error is not None:
        return error

    req = operational_simulator.hall_calls.get(body.request_id)
    if req is None or req.status in ("COMPLETED", "CANCELLED", "FAILED"):
        raise HTTPException(status_code=409, detail="Unknown or terminal hall call")

    acs_res = await FleetAccessControl.validate_emergency_override(
        EmergencyOverridePayload(
            credential_id=session.credential_id,
            override_type=EmergencyOverrideType.CALL_CANCEL,
            target_car_id=req.assigned_car,
            incident_id=body.request_id,
            reason=body.reason,
            timestamp=session.timestamp,
        ),
        db_pool=db_pool,
    )
    if not acs_res.allowed:
        return auth_result_response(acs_res)

    return {
        "source": "labelled-test-stub",
        "request": _perform(
            operational_simulator.cancel_hall_call,
            body.request_id,
            session.credential_id,
            reason=body.reason,
        ),
    }


@router.get("/diagnostics/{elevator_id}")
async def view_diagnostics(
    elevator_id: str,
    session_token: str = Depends(extract_bearer_token),
    db_pool: type[DbConnectionPool] = Depends(get_db_pool),
) -> Any:
    session, error = await _staff(DashboardOperationType.VIEW_DIAGNOSTICS, session_token, db_pool)
    if error is not None:
        return error
    if not ElevatorId.has_value(elevator_id):
        denied = await reject_malformed_msg(
            timestamp=session.timestamp,
            target_id=str(elevator_id),
            source="DiagnosticsPath",
            payload_details={"elevator_id": elevator_id},
            message="Invalid elevator_id.",
            credential_id=session.credential_id,
            db_pool=db_pool,
        )
        return auth_result_response(denied)
    return _perform(operational_simulator.diagnostics, elevator_id)
