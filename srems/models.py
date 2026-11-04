"""Domain models and lifecycle states used by SREMS."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    IDLE = "IDLE"


class DoorState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    OBSTRUCTED = "OBSTRUCTED"


class Availability(str, Enum):
    IN_SERVICE = "IN_SERVICE"
    OCCUPIED = "OCCUPIED"
    MAINTENANCE = "MAINTENANCE"
    FAULT = "FAULT"
    EMERGENCY = "EMERGENCY/UNAVAILABLE"
    STALE = "STALE/UNTRUSTED"


class RequestType(str, Enum):
    HALL = "HALL"
    FREIGHT = "FREIGHT"
    CAR = "CAR"
    SERVICE = "SERVICE"
    RESCUE = "RESCUE"


class RequestStatus(str, Enum):
    RECEIVED = "RECEIVED"
    AUTHORIZATION_CHECK = "AUTHORIZATION_CHECK"
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    APPROACHING = "APPROACHING"
    ARRIVED = "ARRIVED"
    IN_TRANSIT = "IN_TRANSIT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED/REQUIRES_RECONCILIATION"


class Role(str, Enum):
    PASSENGER = "PASSENGER"
    TECHNICIAN = "TECHNICIAN"
    BUILDING_SERVICE = "BUILDING_SERVICE"
    ADMIN = "ADMIN"


class CredentialStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    FALSE_ALARM = "FALSE_ALARM"


@dataclass
class ElevatorState:
    elevator_id: str
    floor: int
    direction: Direction = Direction.IDLE
    moving: bool = False
    door: DoorState = DoorState.CLOSED
    availability: Availability = Availability.IN_SERVICE
    fault: str | None = None
    emergency: str | None = None
    stale: bool = False
    pending_stops: list[int] = field(default_factory=list)
    load_kg: float | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class HallRequest:
    floor: int
    request_type: RequestType
    direction: Direction | None = None
    request_id: str = field(default_factory=lambda: str(uuid4()))
    status: RequestStatus = RequestStatus.RECEIVED
    assigned_elevator: str | None = None
    accepted_at: datetime = field(default_factory=utc_now)
    arrival_at: datetime | None = None
    completed_at: datetime | None = None
    reason: str | None = None
    flags: set[str] = field(default_factory=set)
    event_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CarRequest:
    elevator_id: str
    destination: int
    request_id: str = field(default_factory=lambda: str(uuid4()))
    credential_id: str | None = None
    status: RequestStatus = RequestStatus.RECEIVED
    accepted_at: datetime = field(default_factory=utc_now)
    arrival_at: datetime | None = None
    reason: str | None = None


@dataclass
class Credential:
    credential_id: str
    user_id: str
    role: Role
    allowed_floors: set[int] = field(default_factory=set)
    status: CredentialStatus = CredentialStatus.ACTIVE
    expires_at: datetime | None = None
    suspended_reason: str | None = None
    suspended_at: datetime | None = None


@dataclass
class Incident:
    incident_id: str
    event_type: str
    scope: str
    elevator_id: str | None
    floor: int | None
    direction: Direction | None
    source: str
    created_at: datetime = field(default_factory=utc_now)
    status: IncidentStatus = IncidentStatus.OPEN
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ElevatorCommand:
    elevator_id: str
    command: str
    payload: dict[str, Any]
    issued_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AuditIntegrityResult:
    valid: bool
    first_invalid_sequence: int | None
    entries_checked: int
