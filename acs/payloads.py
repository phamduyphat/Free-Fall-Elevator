from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from acs.enums import (
    AuthorizationOutcome,
    CredentialAction,
    DashboardOperationType,
    DenialReason,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
)


@dataclass
class AuthorizationResult:
    outcome: AuthorizationOutcome
    allowed: bool
    reason_code: Optional[DenialReason | str] = None
    message: str = ""
    session_token: Optional[str] = None
    credential_id: Optional[str] = None
    alert_triggered: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        *,
        message: str = "Access granted.",
        session_token: Optional[str] = None,
        credential_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuthorizationResult:
        return cls(
            outcome=AuthorizationOutcome.ALLOWED,
            allowed=True,
            reason_code=None,
            message=message,
            session_token=session_token,
            credential_id=credential_id,
            alert_triggered=False,
            details=details or {},
        )

    @classmethod
    def deny(
        cls,
        reason_code: DenialReason | str,
        *,
        message: str = "Access denied.",
        credential_id: Optional[str] = None,
        alert_triggered: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> AuthorizationResult:
        return cls(
            outcome=AuthorizationOutcome.DENIED,
            allowed=False,
            reason_code=reason_code,
            message=message,
            session_token=None,
            credential_id=credential_id,
            alert_triggered=alert_triggered,
            details=details or {},
        )


@dataclass
class CardSwipePayload:
    """4.1 & 7.2 Credential Presentation on In-Cabin Reader (`UI-8`) or Dashboard (`UI-3, UI-10`)."""

    elevator_id: Optional[ElevatorId | str] = None
    credential_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    origin_floor: int = 1
    is_dashboard_access: bool = False


@dataclass
class DashboardOperationPayload:
    """7.5 Privileged Dashboard Operation Validation Contract (`SEC-2.13, SEC-2.15, OR-19`)."""

    session_token: str
    op_type: DashboardOperationType | str
    timestamp: datetime = field(default_factory=datetime.now)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CarDestinationRequestPayload:
    """4.2 Passenger Floor Button Press inside an Elevator Cab."""

    elevator_id: ElevatorId | str
    origin_floor: int
    requested_floor: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class E5ServiceRunPayload:
    """4.3 Dedicated Freight Run (`FR-1.13`)."""

    elevator_id: ElevatorId | str
    destination_floor: int
    purpose: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MaintenanceOperationPayload:
    """4.4 Technician Isolation & Diagnostics (`MNT-4.3`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    operation_type: MaintenanceOperationType | str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ReturnToServicePayload:
    """4.5 Restoring Elevator to Service (`MNT-4.6`)."""

    credential_id: str
    elevator_id: ElevatorId | str
    technician_confirmation: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class EmergencyOverridePayload:
    """4.6 Admin Safety Overrides (`EMR-3.8`)."""

    credential_id: str
    override_type: EmergencyOverrideType | str
    target_car_id: Optional[ElevatorId | str]
    incident_id: Optional[str]
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RescueCarDispatchPayload:
    """4.7 Rescue Car Dispatch (`EMR-3.12, EMR-3.16`)."""

    credential_id: str
    incident_id: str
    rescue_car_id: ElevatorId | str
    incident_floor: int
    dwell_wait_seconds: int = 120
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class CredentialManagementPayload:
    """4.8 Admin Credential Lifecycle Operations (`SEC-2.16`)."""

    admin_credential_id: str
    action: CredentialAction | str
    target_user_id: Optional[str] = None
    target_credential_id: Optional[str] = None
    payload_details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RevokeAndReissuePayload:
    """4.9 Atomic Lost Card Replacement (`SEC-2.17, VT-17`)."""

    admin_credential_id: str
    lost_credential_id: str
    replacement_credential_id: str
    reason: str = "LOST_AND_REPLACED"
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class IntegrityCheckResult:
    passed: bool
    total_records_inspected: int
    mismatch_index: Optional[int] = None
    reason: Optional[str] = None

