from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from acs.enums import (
    AuditActionType,
    DenialReason,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
    UserRole,
)
from acs.helpers import (
    authorize_credential_role,
    fail_closed,
    reject_malformed_msg,
)
from acs.logs import Logging
from acs.payloads import (
    AuditEntry,
    AuthorizationResult,
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
)
from db import DbConnectionPool

_ADMIN_ONLY_INCIDENT_KINDS = frozenset(
    {"MEDICAL", "FIRE", "DOOR_OBSTRUCTION", "HELP", "REPEATED_EMERGENCY", "REPEATED_EMPTY_CALLS"}
)

_OVERRIDE_AUDIT_MAP: dict[str, AuditActionType] = {
    EmergencyOverrideType.RESOLVE_INCIDENT.value: AuditActionType.INCIDENT_RESOLVED,
    EmergencyOverrideType.FALSE_ALARM.value: AuditActionType.FALSE_ALARM_CLOSED,
    EmergencyOverrideType.RESCUE_BOARDING.value: AuditActionType.RESCUE_CAR_BOARDED,
    EmergencyOverrideType.RESCUE_RELEASE.value: AuditActionType.RESCUE_CAR_RELEASED,
    EmergencyOverrideType.CALL_CANCEL.value: AuditActionType.CALL_CANCELLED_BY_ADMIN,
}


class FleetAccessControl:
    """
    ACS.FleetAccessControl submodule (`acs/fleet_accesscontrol.py`).
    Validates authentication and role authorization for fleet maintenance and
    emergency operations (`validate_maintenance_operation`, `validate_return_to_service`,
    `validate_emergency_override`, and `validate_rescue_car_dispatch`).
    """

    @staticmethod
    async def validate_maintenance_operation(
        payload: MaintenanceOperationPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(
                    payload.elevator_id
                ) or not MaintenanceOperationType.has_value(payload.operation_type):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.elevator_id),
                        source="MaintenanceOperationPayload",
                        payload_details={
                            "elevator_id": str(payload.elevator_id),
                            "operation_type": str(payload.operation_type),
                        },
                        message="Invalid elevator_id or operation_type.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    required_roles=[UserRole.TECHNICIAN.value, UserRole.ADMIN.value],
                    inactive_msg="Active technician or admin credential required.",
                    unauth_msg=f"Role '{{role}}' cannot execute maintenance operation '{payload.operation_type}'.",
                    target_id=str(payload.elevator_id),
                    operation=str(payload.operation_type),
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

                audit_action = (
                    AuditActionType.DIAGNOSTIC_QUERY
                    if str(payload.operation_type) == MaintenanceOperationType.DIAGNOSTIC_QUERY.value
                    else AuditActionType.MAINTENANCE_ENTER
                )
                await Logging.write_audit(
                    [
                        AuditEntry(
                            timestamp=ts,
                            actor_id=payload.credential_id,
                            actor_role=role,
                            action_type=audit_action,
                            target_id=str(payload.elevator_id),
                            event_payload={
                                "elevator_id": str(payload.elevator_id),
                                "operation_type": str(payload.operation_type),
                            },
                        )
                    ],
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Maintenance operation '{payload.operation_type}' authorized for {payload.elevator_id}.",
                    credential_id=payload.credential_id,
                    details={
                        "elevator_id": str(payload.elevator_id),
                        "operation_type": str(payload.operation_type),
                        "role": role,
                    },
                )
        except Exception:
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_return_to_service(
        payload: ReturnToServicePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not ElevatorId.has_value(payload.elevator_id):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.elevator_id),
                        source="ReturnToServicePayload",
                        payload_details={"elevator_id": str(payload.elevator_id)},
                        message="Invalid elevator_id.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    required_roles=[UserRole.TECHNICIAN.value, UserRole.ADMIN.value],
                    inactive_msg="Active technician or admin credential required.",
                    unauth_msg="Role '{role}' is not authorized to return car to service.",
                    target_id=str(payload.elevator_id),
                    operation=AuditActionType.RETURN_TO_SERVICE.value,
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

                if not payload.technician_confirmation:
                    return AuthorizationResult.deny(
                        DenialReason.CONFIRMATION_REQUIRED,
                        message="Technician safety confirmation is required to return elevator to service.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
                    [
                        AuditEntry(
                            timestamp=ts,
                            actor_id=payload.credential_id,
                            actor_role=role,
                            action_type=AuditActionType.RETURN_TO_SERVICE,
                            target_id=str(payload.elevator_id),
                            event_payload={
                                "elevator_id": str(payload.elevator_id),
                                "technician_confirmation": payload.technician_confirmation,
                            },
                        )
                    ],
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Return to service authorized for {payload.elevator_id}.",
                    credential_id=payload.credential_id,
                    details={"elevator_id": str(payload.elevator_id), "role": role},
                )
        except Exception:
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_emergency_override(
        payload: EmergencyOverridePayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                if not EmergencyOverrideType.has_value(payload.override_type):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.target_car_id or payload.incident_id or "UNKNOWN"),
                        source="EmergencyOverridePayload",
                        payload_details={"override_type": str(payload.override_type)},
                        message=f"Invalid override_type '{payload.override_type}'.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                target_entity = str(payload.target_car_id or payload.incident_id or UserRole.SYSTEM)
                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required.",
                    unauth_msg=f"Role '{{role}}' cannot execute emergency override '{payload.override_type}'.",
                    target_id=target_entity,
                    operation=str(payload.override_type),
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

                audit_action = _OVERRIDE_AUDIT_MAP.get(
                    str(payload.override_type), AuditActionType.EMERGENCY_OVERRIDE
                )
                await Logging.write_audit(
                    [
                        AuditEntry(
                            timestamp=ts,
                            actor_id=payload.credential_id,
                            actor_role=role,
                            action_type=audit_action,
                            target_id=target_entity,
                            event_payload={
                                "override_type": str(payload.override_type),
                                "target_car_id": (
                                    str(payload.target_car_id) if payload.target_car_id else None
                                ),
                                "incident_id": payload.incident_id,
                                "reason": payload.reason,
                            },
                        )
                    ],
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Emergency override '{payload.override_type}' authorized.",
                    credential_id=payload.credential_id,
                    details={
                        "override_type": str(payload.override_type),
                        "target_car_id": (
                            str(payload.target_car_id) if payload.target_car_id else None
                        ),
                        "incident_id": payload.incident_id,
                    },
                )
        except Exception:
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_rescue_car_dispatch(
        payload: RescueCarDispatchPayload,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        ts = Logging.normalize_timestamp(payload.timestamp)
        try:
            async with db_pool.transaction() as conn:
                record, denial = await authorize_credential_role(
                    conn,
                    payload.credential_id,
                    ts,
                    required_roles=[UserRole.ADMIN.value],
                    inactive_msg="Active Admin credential required.",
                    unauth_msg="Role '{role}' is not authorized to dispatch a rescue car.",
                    target_id=str(payload.rescue_car_id),
                    operation=AuditActionType.RESCUE_CAR_DISPATCHED.value,
                    extra_payload={"incident_id": payload.incident_id},
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

                if not ElevatorId.has_value(payload.rescue_car_id):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.rescue_car_id),
                        source="RescueCarDispatchPayload",
                        payload_details={
                            "rescue_car_id": str(payload.rescue_car_id),
                            "incident_id": payload.incident_id,
                        },
                        message=f"Invalid rescue_car_id '{payload.rescue_car_id}'.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                if not ElevatorId.is_residential(payload.rescue_car_id):
                    await Logging.write_audit(
                        [
                            AuditEntry(
                                timestamp=ts,
                                actor_id=payload.credential_id,
                                actor_role=role,
                                action_type=AuditActionType.COMMAND_CONFLICT_REJECTED,
                                target_id=str(payload.rescue_car_id),
                                event_payload={
                                    "attempted_command": AuditActionType.RESCUE_CAR_DISPATCHED.value,
                                    "rescue_car_id": str(payload.rescue_car_id),
                                    "incident_id": payload.incident_id,
                                    "conflict_reason": "E5_INELIGIBLE_AS_RESCUE_CAR",
                                },
                            )
                        ],
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.COMMAND_CONFLICT_REJECTED,
                        message="Rescue car must be an eligible residential car (E1-E4); E5 cannot be used.",
                        credential_id=payload.credential_id,
                    )

                from simulation.operational import operational_simulator

                conflict_reason: Optional[str] = None
                conflict_msg = ""
                with operational_simulator._lock:
                    car_state = operational_simulator.cars.get(str(payload.rescue_car_id))
                    if car_state is not None:
                        if car_state.availability != "IN_SERVICE":
                            conflict_reason = f"CAR_IN_{car_state.availability}"
                            conflict_msg = (
                                f"Rescue dispatch rejected: car {payload.rescue_car_id} "
                                f"is in {car_state.availability}."
                            )
                        elif not car_state.connected:
                            conflict_reason = "CAR_DISCONNECTED"
                            conflict_msg = f"Rescue dispatch rejected: car {payload.rescue_car_id} is disconnected."
                        elif car_state.rescue_active or car_state.medical_run:
                            conflict_reason = "CAR_BUSY_EMERGENCY"
                            conflict_msg = f"Rescue dispatch rejected: car {payload.rescue_car_id} is busy on an emergency run."
                        elif any(s.kind != "HALL" for s in car_state.stops):
                            conflict_reason = "CAR_NOT_EMPTY"
                            conflict_msg = f"Rescue dispatch rejected: car {payload.rescue_car_id} has active car commitments."

                if conflict_reason is not None:
                    await Logging.write_audit(
                        [
                            AuditEntry(
                                timestamp=ts,
                                actor_id=payload.credential_id,
                                actor_role=role,
                                action_type=AuditActionType.COMMAND_CONFLICT_REJECTED,
                                target_id=str(payload.rescue_car_id),
                                event_payload={
                                    "attempted_command": AuditActionType.RESCUE_CAR_DISPATCHED.value,
                                    "rescue_car_id": str(payload.rescue_car_id),
                                    "incident_id": payload.incident_id,
                                    "conflict_reason": conflict_reason,
                                },
                            )
                        ],
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.COMMAND_CONFLICT_REJECTED,
                        message=conflict_msg,
                        credential_id=payload.credential_id,
                    )

                if not (1 <= payload.dwell_wait_seconds <= 600):
                    return await reject_malformed_msg(
                        timestamp=ts,
                        target_id=str(payload.rescue_car_id),
                        source="RescueCarDispatchPayload",
                        payload_details={
                            "rescue_car_id": str(payload.rescue_car_id),
                            "incident_id": payload.incident_id,
                            "dwell_wait_seconds": payload.dwell_wait_seconds,
                        },
                        message="dwell_wait_seconds must be between 1 and 600 seconds.",
                        credential_id=payload.credential_id,
                        db_pool=db_pool,
                    )

                await Logging.write_audit(
                    [
                        AuditEntry(
                            timestamp=ts,
                            actor_id=payload.credential_id,
                            actor_role=role,
                            action_type=AuditActionType.RESCUE_CAR_DISPATCHED,
                            target_id=str(payload.rescue_car_id),
                            event_payload={
                                "rescue_car_id": str(payload.rescue_car_id),
                                "incident_id": payload.incident_id,
                                "incident_floor": payload.incident_floor,
                                "dwell_wait_seconds": payload.dwell_wait_seconds,
                            },
                        )
                    ],
                    db_pool=db_pool,
                )
                return AuthorizationResult.allow(
                    message=f"Rescue car {payload.rescue_car_id} dispatch authorized for incident {payload.incident_id}.",
                    credential_id=payload.credential_id,
                    details={
                        "rescue_car_id": str(payload.rescue_car_id),
                        "incident_id": payload.incident_id,
                        "incident_floor": payload.incident_floor,
                        "dwell_wait_seconds": payload.dwell_wait_seconds,
                    },
                )
        except Exception:
            return fail_closed(payload.credential_id)

    @staticmethod
    async def validate_incident_action(
        *,
        credential_id: str,
        incident_id: str,
        incident_kind: str,
        elevator_id: Optional[str],
        action: str,
        recovery_confirmed: bool,
        returning_cars: Optional[list[str]] = None,
        precheck: Optional[Callable[[], list[str]]] = None,
        timestamp: datetime,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> AuthorizationResult:
        """Validate role authority and write tamper-evident audit records for incident lifecycle actions."""
        ts = Logging.normalize_timestamp(timestamp)
        if action not in ("ACKNOWLEDGE", "RESOLVE", "CLOSE", "FALSE_ALARM"):
            return await reject_malformed_msg(
                timestamp=ts,
                target_id=incident_id or "UNKNOWN",
                source="IncidentAction",
                payload_details={"incident_id": incident_id, "action": action},
                message=f"Invalid incident action '{action}'.",
                credential_id=credential_id,
                db_pool=db_pool,
            )

        admin_required = action in ("RESOLVE", "CLOSE", "FALSE_ALARM") and (
            incident_kind in _ADMIN_ONLY_INCIDENT_KINDS or action == "FALSE_ALARM"
        )
        required_roles = (
            [UserRole.ADMIN.value]
            if admin_required
            else [UserRole.TECHNICIAN.value, UserRole.ADMIN.value]
        )

        try:
            async with db_pool.transaction() as conn:
                record, denial = await authorize_credential_role(
                    conn,
                    credential_id,
                    ts,
                    required_roles=required_roles,
                    inactive_msg="Active staff credential required for incident action.",
                    unauth_msg=(
                        f"Role '{{role}}' is not authorized to perform '{action}' "
                        f"on incident type '{incident_kind}'."
                    ),
                    target_id=incident_id,
                    operation=f"INCIDENT_{action}",
                    extra_payload={"incident_kind": incident_kind, "elevator_id": elevator_id},
                    db_pool=db_pool,
                )
                if denial:
                    return denial
                assert record is not None
                role = record["user_role"]

                if action == "RESOLVE" and incident_kind != "HELP" and not recovery_confirmed:
                    return AuthorizationResult.deny(
                        DenialReason.CONFIRMATION_REQUIRED,
                        message="Recovery confirmation is required to resolve this incident.",
                        credential_id=credential_id,
                    )

                if precheck is not None:
                    resolved_returning_cars = precheck()
                else:
                    resolved_returning_cars = returning_cars or []

                action_map = {
                    "ACKNOWLEDGE": AuditActionType.INCIDENT_ACKNOWLEDGED,
                    "RESOLVE": AuditActionType.INCIDENT_RESOLVED,
                    "CLOSE": AuditActionType.INCIDENT_CLOSED,
                    "FALSE_ALARM": AuditActionType.FALSE_ALARM_CLOSED,
                }
                entries = [
                    AuditEntry(
                        timestamp=ts,
                        actor_id=credential_id,
                        actor_role=role,
                        action_type=action_map[action],
                        target_id=incident_id,
                        event_payload={
                            "incident_id": incident_id,
                            "incident_kind": incident_kind,
                            "elevator_id": elevator_id,
                            "action": action,
                            "recovery_confirmed": recovery_confirmed,
                        },
                    )
                ]
                for car_id in resolved_returning_cars:
                    entries.append(
                        AuditEntry(
                            timestamp=ts,
                            actor_id=credential_id,
                            actor_role=role,
                            action_type=AuditActionType.RETURN_TO_SERVICE,
                            target_id=car_id,
                            event_payload={
                                "elevator_id": car_id,
                                "incident_id": incident_id,
                                "technician_confirmation": recovery_confirmed,
                            },
                        )
                    )
                await Logging.write_audit(entries, db_pool=db_pool)
                return AuthorizationResult.allow(
                    message=f"Incident action '{action}' authorized.",
                    credential_id=credential_id,
                    details={
                        "incident_id": incident_id,
                        "action": action,
                        "role": role,
                    },
                )
        except Exception as exc:
            from simulation.operational import OperationalError

            if isinstance(exc, OperationalError):
                raise
            return fail_closed(credential_id)

