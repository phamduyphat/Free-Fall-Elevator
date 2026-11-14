from acs.cab_accesscontrol import CabAccessControl
from acs.credential_accesscontrol import CredentialAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    CredentialAction,
    CredentialStatus,
    CredentialType,
    DashboardOperationType,
    DenialReason,
    ElevatorId,
    EmergencyOverrideType,
    MaintenanceOperationType,
    SuspensionReason,
    UserRole,
)
from acs.fleet_accesscontrol import FleetAccessControl
from acs.logs import Logging
from acs.payloads import (
    AuthorizationResult,
    CardSwipePayload,
    CarDestinationRequestPayload,
    CredentialManagementPayload,
    DashboardOperationPayload,
    E5ServiceRunPayload,
    EmergencyOverridePayload,
    IntegrityCheckResult,
    MaintenanceOperationPayload,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
    RevokeAndReissuePayload,
)


class ACS:
    """Namespace facade exposing `CabAccessControl`, `DashboardAccessControl`, `CredentialAccessControl`, `FleetAccessControl`, and `Logging`."""

    CabAccessControl = CabAccessControl
    CredentialAccessControl = CredentialAccessControl
    DashboardAccessControl = DashboardAccessControl
    FleetAccessControl = FleetAccessControl
    Logging = Logging
