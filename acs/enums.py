from __future__ import annotations

from enum import StrEnum
from typing import Optional


class ElevatorId(StrEnum):
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_

    @classmethod
    def is_residential(cls, value: Optional[str]) -> bool:
        return value in (cls.E1, cls.E2, cls.E3, cls.E4)


class UserRole(StrEnum):
    RESIDENT = "RESIDENT"
    VISITOR = "VISITOR"
    BUILDING_SERVICE = "BUILDING_SERVICE"
    TECHNICIAN = "TECHNICIAN"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"

    @classmethod
    def can_initiate_service_run(cls, role: str) -> bool:
        return role in (cls.BUILDING_SERVICE, cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def can_request_e5_freight(cls, role: str) -> bool:
        return role in (cls.RESIDENT, cls.BUILDING_SERVICE, cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def can_perform_maintenance(cls, role: str) -> bool:
        return role in (cls.TECHNICIAN, cls.ADMIN)

    @classmethod
    def is_admin(cls, role: str) -> bool:
        return role == cls.ADMIN


class CredentialType(StrEnum):
    RESIDENT = "RESIDENT"
    VISITOR = "VISITOR"
    STAFF = "STAFF"
    TECHNICIAN = "TECHNICIAN"
    ADMIN = "ADMIN"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class SuspensionReason(StrEnum):
    LOST = "LOST"
    CLONE_SUSPECTED = "CLONE_SUSPECTED"
    ADMIN_MANUAL = "ADMIN_MANUAL"


class MaintenanceOperationType(StrEnum):
    ENTER_MAINTENANCE = "ENTER_MAINTENANCE"
    DIAGNOSTIC_QUERY = "DIAGNOSTIC_QUERY"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


class EmergencyOverrideType(StrEnum):
    DOOR_OVERRIDE = "DOOR_OVERRIDE"
    MANUAL_RECALL = "MANUAL_RECALL"
    RESOLVE_INCIDENT = "RESOLVE_INCIDENT"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


class CredentialAction(StrEnum):
    ISSUE = "ISSUE"
    SUSPEND = "SUSPEND"
    REINSTATE = "REINSTATE"
    REVOKE = "REVOKE"
    ISSUE_VISITOR_PASS = "ISSUE_VISITOR_PASS"


class DashboardOperationType(StrEnum):
    VIEW_FLEET_STATUS = "VIEW_FLEET_STATUS"
    VIEW_DIAGNOSTICS = "VIEW_DIAGNOSTICS"
    MAINTENANCE_CONTROL = "MAINTENANCE_CONTROL"
    MANAGE_CREDENTIALS = "MANAGE_CREDENTIALS"
    EMERGENCY_OVERRIDE = "EMERGENCY_OVERRIDE"
    VIEW_AUDIT_LOGS = "VIEW_AUDIT_LOGS"
    RUN_AUDIT_INTEGRITY = "RUN_AUDIT_INTEGRITY"
    EXPORT_ANALYTICS_REPORTS = "EXPORT_ANALYTICS_REPORTS"

    @classmethod
    def has_value(cls, value: Optional[str]) -> bool:
        return value in cls._value2member_map_


DASHBOARD_PERMISSIONS: dict[str, dict[DashboardOperationType, bool]] = {
    UserRole.ADMIN: {op: True for op in DashboardOperationType},
    UserRole.TECHNICIAN: {
        DashboardOperationType.VIEW_FLEET_STATUS: True,
        DashboardOperationType.VIEW_DIAGNOSTICS: True,
        DashboardOperationType.MAINTENANCE_CONTROL: True,
        DashboardOperationType.MANAGE_CREDENTIALS: False,
        DashboardOperationType.EMERGENCY_OVERRIDE: False,
        DashboardOperationType.VIEW_AUDIT_LOGS: False,
        DashboardOperationType.RUN_AUDIT_INTEGRITY: False,
        DashboardOperationType.EXPORT_ANALYTICS_REPORTS: False,
    },
    UserRole.RESIDENT: {op: False for op in DashboardOperationType},
    UserRole.VISITOR: {op: False for op in DashboardOperationType},
    UserRole.BUILDING_SERVICE: {op: False for op in DashboardOperationType},
}


ROLE_TO_CREDENTIAL_TYPE: dict[UserRole, CredentialType] = {
    UserRole.RESIDENT: CredentialType.RESIDENT,
    UserRole.VISITOR: CredentialType.VISITOR,
    UserRole.BUILDING_SERVICE: CredentialType.STAFF,
    UserRole.TECHNICIAN: CredentialType.TECHNICIAN,
    UserRole.ADMIN: CredentialType.ADMIN,
}


class AuthorizationOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class DenialReason(StrEnum):
    UNREGISTERED_CARD = "UNREGISTERED_CARD"
    CARD_SUSPENDED = "CARD_SUSPENDED"
    CARD_REVOKED = "CARD_REVOKED"
    CARD_EXPIRED = "CARD_EXPIRED"
    UNAUTHORIZED_FLOOR = "UNAUTHORIZED_FLOOR"
    E5_B1_FORBIDDEN = "E5_B1_FORBIDDEN"
    ONE_TAP_LIMIT_EXCEEDED = "ONE_TAP_LIMIT_EXCEEDED"
    TOKEN_WINDOW_EXPIRED = "TOKEN_WINDOW_EXPIRED"
    NO_ACTIVE_CREDENTIAL = "NO_ACTIVE_CREDENTIAL"
    DB_FAIL_CLOSED = "DB_FAIL_CLOSED"
    MALFORMED_MSG_REJECTED = "MALFORMED_MSG_REJECTED"
    UNAUTHORIZED_CMD_REJECTED = "UNAUTHORIZED_CMD_REJECTED"
    COMMAND_CONFLICT_REJECTED = "COMMAND_CONFLICT_REJECTED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    PEER_ADMIN_REQUIRED = "PEER_ADMIN_REQUIRED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    CREDENTIAL_ALREADY_EXISTS = "CREDENTIAL_ALREADY_EXISTS"
    REPLACEMENT_ID_IN_USE = "REPLACEMENT_ID_IN_USE"
    DASHBOARD_ACCESS_DENIED = "DASHBOARD_ACCESS_DENIED"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    UNAUTHORIZED_ROLE = "UNAUTHORIZED_ROLE"


class AlertSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertType(StrEnum):
    REPEATED_FAILURES = "REPEATED_FAILURES"
    CLONED_CREDENTIAL = "CLONED_CREDENTIAL"
    CARD_SCANNING = "CARD_SCANNING"
    LOST_CARD_USAGE = "LOST_CARD_USAGE"
    REVOKED_CARD_USAGE = "REVOKED_CARD_USAGE"
    DOOR_OBSTRUCTION = "DOOR_OBSTRUCTION"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AuditActionType(StrEnum):
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOGOUT = "ADMIN_LOGOUT"
    ACCESS_DENIAL = "ACCESS_DENIAL"
    ONE_TAP_VIOLATION = "ONE_TAP_VIOLATION"
    CREDENTIAL_ISSUED = "CREDENTIAL_ISSUED"
    CREDENTIAL_SUSPENDED = "CREDENTIAL_SUSPENDED"
    CREDENTIAL_REINSTATED = "CREDENTIAL_REINSTATED"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    CREDENTIAL_REPLACED = "CREDENTIAL_REPLACED"
    VISITOR_PASS_ISSUED = "VISITOR_PASS_ISSUED"
    VISITOR_PASS_EXPIRED = "VISITOR_PASS_EXPIRED"
    POLICY_CHANGED = "POLICY_CHANGED"
    CALL_CANCELLED_BY_ADMIN = "CALL_CANCELLED_BY_ADMIN"
    E5_SERVICE_RUN_START = "E5_SERVICE_RUN_START"
    E5_SERVICE_RUN_END = "E5_SERVICE_RUN_END"
    EMERGENCY_OVERRIDE = "EMERGENCY_OVERRIDE"
    RESCUE_CAR_DISPATCHED = "RESCUE_CAR_DISPATCHED"
    RESCUE_CAR_BOARDED = "RESCUE_CAR_BOARDED"
    RESCUE_CAR_RELEASED = "RESCUE_CAR_RELEASED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    FALSE_ALARM_CLOSED = "FALSE_ALARM_CLOSED"
    MAINTENANCE_ENTER = "MAINTENANCE_ENTER"
    RETURN_TO_SERVICE = "RETURN_TO_SERVICE"
    MALFORMED_MSG_REJECTED = "MALFORMED_MSG_REJECTED"
    COMMAND_CONFLICT_REJECTED = "COMMAND_CONFLICT_REJECTED"
    UNAUTHORIZED_CMD_REJECTED = "UNAUTHORIZED_CMD_REJECTED"
    INTEGRITY_CHECK_RUN = "INTEGRITY_CHECK_RUN"
    REPORT_EXPORTED = "REPORT_EXPORTED"
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    DATA_BACKUP_CREATED = "DATA_BACKUP_CREATED"
    DATA_RESTORE_COMPLETED = "DATA_RESTORE_COMPLETED"

