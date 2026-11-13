from __future__ import annotations

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
    AuthorizationResult,
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
)
from db import DbConnectionPool


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

                await Logging.write_audit(
                    timestamp=ts,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=AuditActionType.MAINTENANCE_ENTER,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "elevator_id": str(payload.elevator_id),
                        "operation_type": str(payload.operation_type),
                    },
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
                    timestamp=ts,
                    actor_id=payload.credential_id,
                    actor_role=role,
                    action_type=AuditActionType.RETURN_TO_SERVICE,
                    target_id=str(payload.elevator_id),
                    event_payload={
                        "elevator_id": str(payload.elevator_id),
                        "technician_confirmation": payload.technician_confirmation,
                    },
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

                audit_action = (
                    AuditActionType.INCIDENT_RESOLVED
                    if payload.override_type == EmergencyOverrideType.RESOLVE_INCIDENT
                    else AuditActionType.EMERGENCY_OVERRIDE
                )
                await Logging.write_audit(
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

                if not ElevatorId.is_residential(payload.rescue_car_id):
                    await Logging.write_audit(
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
                        db_pool=db_pool,
                    )
                    return AuthorizationResult.deny(
                        DenialReason.COMMAND_CONFLICT_REJECTED,
                        message="Rescue car must be an eligible residential car (E1-E4); E5 cannot be used.",
                        credential_id=payload.credential_id,
                    )

                if not (1 <= payload.dwell_wait_seconds <= 600):
                    return AuthorizationResult.deny(
                        DenialReason.MALFORMED_MSG_REJECTED,
                        message="dwell_wait_seconds must be between 1 and 600 seconds.",
                        credential_id=payload.credential_id,
                    )

                await Logging.write_audit(
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

