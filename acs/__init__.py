from acs.accesscontrol import (
    AccessControl,
    AuthorizationResult,
    CardSwipePayload,
    CarDestinationRequestPayload,
    CredentialManagementPayload,
    E5ServiceRunPayload,
    EmergencyOverridePayload,
    MaintenanceOperationPayload,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
    RevokeAndReissuePayload,
)
from acs.logs import IntegrityCheckResult, Logging


class ACS:
    """Namespace facade exposing `ACS.AccessControl` and `ACS.Logging`."""

    AccessControl = AccessControl
    Logging = Logging


__all__ = [
    "ACS",
    "AccessControl",
    "AuthorizationResult",
    "CardSwipePayload",
    "CarDestinationRequestPayload",
    "CredentialManagementPayload",
    "E5ServiceRunPayload",
    "EmergencyOverridePayload",
    "IntegrityCheckResult",
    "Logging",
    "MaintenanceOperationPayload",
    "RescueCarDispatchPayload",
    "ReturnToServicePayload",
    "RevokeAndReissuePayload",
]
