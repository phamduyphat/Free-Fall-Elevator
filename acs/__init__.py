from acs.accesscontrol import (
    AccessControl,
    AuthorizationResult,
    CardSwipePayload,
    CarDestinationRequestPayload,
    CredentialAction,
    CredentialManagementPayload,
    CredentialStatus,
    CredentialType,
    DashboardOperationPayload,
    DashboardOperationType,
    E5ServiceRunPayload,
    ElevatorId,
    EmergencyOverridePayload,
    EmergencyOverrideType,
    MaintenanceOperationPayload,
    MaintenanceOperationType,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
    RevokeAndReissuePayload,
    SuspensionReason,
    UserRole,
)
from acs.logs import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    DenialReason,
    IntegrityCheckResult,
    Logging,
)


class ACS:
    """Namespace facade exposing `ACS.AccessControl` and `ACS.Logging`."""

    AccessControl = AccessControl
    Logging = Logging
