from acs.cab_accesscontrol import CabAccessControl
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
    """Namespace facade exposing `ACS.CabAccessControl`, `ACS.DashboardAccessControl`, and `ACS.Logging`."""

    CabAccessControl = CabAccessControl
    DashboardAccessControl = DashboardAccessControl
    Logging = Logging
