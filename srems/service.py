"""SREMS application service coordinating dispatch, access, and operations."""

from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import median
from typing import Any
from uuid import uuid4

from .access import AccessControl
from .audit import AuditLog
from .config import SREMSConfig
from .dispatch import choose_elevator, is_eligible
from .elevator_interface import (
    ElevatorInterface,
    InterfaceUnavailable,
    SimulatedElevatorInterface,
)
from .models import (
    Availability,
    CarRequest,
    Credential,
    CredentialStatus,
    Direction,
    DoorState,
    ElevatorState,
    HallRequest,
    Incident,
    IncidentStatus,
    RequestStatus,
    RequestType,
    Role,
    utc_now,
)
from .storage import SQLiteStore


class SREMS:
    """Software-only SREMS prototype; the default elevator interface is a test stub."""

    _EMERGENCY_PRIORITY = {
        "OVERLOAD": 1,
        "DOOR_OBSTRUCTION": 2,
        "STUCK": 3,
        "MEDICAL": 4,
        "COMMUNICATION_FAILURE": 5,
        "POWER_FAILURE": 6,
        "FIRE": 7,
    }

    def __init__(
        self,
        config: SREMSConfig | None = None,
        store: SQLiteStore | None = None,
        interface: ElevatorInterface | None = None,
    ) -> None:
        self.config = config or SREMSConfig()
        self.store = store or SQLiteStore()
        self.audit = AuditLog(self.store)
        self.access = AccessControl(self.config)
        self.states = {
            elevator.elevator_id: ElevatorState(
                elevator_id=elevator.elevator_id,
                floor=1,
            )
            for elevator in self.config.elevators
        }
        self.interface = interface or SimulatedElevatorInterface(self.states)
        if isinstance(self.interface, SimulatedElevatorInterface):
            for state in self.states.values():
                self.interface.publish_state(state)
        self.hall_requests: dict[str, HallRequest] = {}
        self.car_requests: dict[str, CarRequest] = {}
        self.incidents: dict[str, Incident] = {}
        self.alerts: list[dict[str, Any]] = []
        self._failed_auth: dict[str, deque[datetime]] = defaultdict(deque)
        self._presentations: dict[str, deque[tuple[datetime, str]]] = defaultdict(deque)
        self._unknown_cards: deque[tuple[datetime, str]] = deque()
        self._emergency_triggers: dict[str, deque[datetime]] = defaultdict(deque)
        self._obstructions: dict[str, datetime] = {}
        self._obstruction_alerted: set[str] = set()
        self._arrived_hall_requests: dict[str, tuple[datetime, str, bool]] = {}
        self._no_boarding_events: dict[int, deque[datetime]] = defaultdict(deque)
        self._dashboard_activity: dict[str, datetime] = {}
        self._building_emergency: set[str] = set()
        self._medical_cars: set[str] = set()

    def _record(
        self,
        event_type: str,
        details: dict[str, Any],
        category: str = "operations",
        at: datetime | None = None,
    ) -> int:
        return self.store.record_event(category, event_type, details, at or utc_now())

    def _audit(
        self,
        actor: str | None,
        action: str,
        outcome: str,
        details: dict[str, Any],
        at: datetime | None = None,
    ) -> int:
        return self.audit.record(actor or "SYSTEM", action, outcome, details, at or utc_now())

    def _alert(
        self,
        event_type: str,
        severity: str,
        details: dict[str, Any],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        alert = {
            "alert_id": str(uuid4()),
            "event_type": event_type,
            "severity": severity,
            "timestamp": (at or utc_now()).isoformat(),
            **details,
        }
        self.alerts.append(alert)
        self._record("security_alert", alert, "security", at)
        return alert

    def _actor(
        self,
        credential_id: str | None,
        allowed_roles: set[Role],
        action: str,
        now: datetime,
    ) -> Credential | None:
        self.expire_credentials(now)
        credential = self.access.credentials.get(credential_id or "")
        allowed = self.access.has_role(credential_id, allowed_roles, now)
        if not allowed:
            self._audit(
                credential_id,
                action,
                "REJECTED",
                {"reason": "Insufficient role or inactive credential."},
                now,
            )
            raise PermissionError("An active credential with the required role is required.")
        return credential

    def expire_credentials(self, now: datetime | None = None) -> list[str]:
        current_time = now or utc_now()
        expired = []
        for credential in self.access.credentials.values():
            if (
                credential.status == CredentialStatus.ACTIVE
                and credential.expires_at is not None
                and credential.expires_at <= current_time
            ):
                credential.status = CredentialStatus.EXPIRED
                expired.append(credential.credential_id)
                self._record(
                    "credential_expired",
                    {"credential_id": credential.credential_id},
                    "security",
                    current_time,
                )
                self._audit(
                    "SYSTEM",
                    "credential_expired",
                    "SUCCESS",
                    {"credential_id": credential.credential_id},
                    current_time,
                )
        return expired

    @staticmethod
    def _active_admin_exists(access: AccessControl) -> bool:
        return any(item.role == Role.ADMIN for item in access.credentials.values())

    def bootstrap_admin(
        self, credential_id: str, user_id: str, now: datetime | None = None
    ) -> Credential:
        """Create the initial admin exactly once; subsequent admin creation is role-gated."""
        current_time = now or utc_now()
        if self._active_admin_exists(self.access):
            raise PermissionError("An administrator already exists; use issue_credential.")
        credential = Credential(credential_id, user_id, Role.ADMIN, set(range(1, 31)))
        self.access.register(credential)
        self._record(
            "credential_issued",
            {"credential_id": credential_id, "user_id": user_id, "role": Role.ADMIN.value},
            "security",
            current_time,
        )
        self._audit(
            user_id,
            "bootstrap_admin",
            "SUCCESS",
            {"credential_id": credential_id},
            current_time,
        )
        return credential

    def issue_credential(
        self,
        actor_id: str,
        credential_id: str,
        user_id: str,
        role: Role,
        allowed_floors: set[int] | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Credential:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "issue_credential", current_time)
        credential = Credential(
            credential_id=credential_id,
            user_id=user_id,
            role=role,
            allowed_floors=set(allowed_floors or ()),
            expires_at=expires_at,
        )
        self.access.register(credential)
        self._record(
            "credential_issued",
            {
                "credential_id": credential_id,
                "user_id": user_id,
                "role": role.value,
                "allowed_floors": sorted(credential.allowed_floors),
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            "security",
            current_time,
        )
        self._audit(
            actor_id,
            "issue_credential",
            "SUCCESS",
            {"credential_id": credential_id, "user_id": user_id, "role": role.value},
            current_time,
        )
        return credential

    def issue_visitor_pass(
        self,
        actor_id: str,
        credential_id: str,
        visitor_name: str,
        allowed_floors: set[int],
        now: datetime | None = None,
    ) -> Credential:
        current_time = now or utc_now()
        return self.issue_credential(
            actor_id,
            credential_id,
            visitor_name,
            Role.PASSENGER,
            allowed_floors,
            current_time + timedelta(hours=self.config.visitor_pass_hours),
            current_time,
        )

    def suspend_credential(
        self,
        actor_id: str,
        credential_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "suspend_credential", current_time)
        if reason not in {"LOST", "CLONE_SUSPECTED"}:
            raise ValueError("Suspension reason must be LOST or CLONE_SUSPECTED.")
        credential = self.access.credentials.get(credential_id)
        if credential is None or credential.status != CredentialStatus.ACTIVE:
            raise ValueError("Only an active registered credential can be suspended.")
        credential.status = CredentialStatus.SUSPENDED
        credential.suspended_reason = reason
        credential.suspended_at = current_time
        self._record(
            "credential_suspended",
            {"credential_id": credential_id, "reason": reason},
            "security",
            current_time,
        )
        self._audit(
            actor_id,
            "suspend_credential",
            "SUCCESS",
            {"credential_id": credential_id, "reason": reason},
            current_time,
        )

    def reinstate_credential(
        self,
        actor_id: str,
        credential_id: str,
        identity_confirmed: bool,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "reinstate_credential", current_time)
        credential = self.access.credentials.get(credential_id)
        if (
            credential is None
            or credential.status != CredentialStatus.SUSPENDED
            or not identity_confirmed
        ):
            raise ValueError("A suspended credential and confirmed identity are required.")
        credential.status = CredentialStatus.ACTIVE
        credential.suspended_reason = None
        credential.suspended_at = None
        self._record(
            "credential_reinstated",
            {"credential_id": credential_id},
            "security",
            current_time,
        )
        self._audit(
            actor_id,
            "reinstate_credential",
            "SUCCESS",
            {"credential_id": credential_id},
            current_time,
        )

    def revoke_credential(
        self,
        actor_id: str,
        credential_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "revoke_credential", current_time)
        credential = self.access.credentials.get(credential_id)
        if credential is None:
            raise ValueError("Credential is not registered.")
        if credential.status == CredentialStatus.REVOKED:
            raise ValueError("A revoked credential cannot be reinstated or revoked again.")
        credential.status = CredentialStatus.REVOKED
        self._record(
            "credential_revoked",
            {"credential_id": credential_id},
            "security",
            current_time,
        )
        self._audit(
            actor_id,
            "revoke_credential",
            "SUCCESS",
            {"credential_id": credential_id},
            current_time,
        )

    def report_lost_credential(
        self,
        actor_id: str,
        credential_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self.suspend_credential(actor_id, credential_id, "LOST", current_time)
        credential = self.access.credentials[credential_id]
        credential.expires_at = current_time + timedelta(
            hours=self.config.lost_card_review_hours
        )

    def replace_lost_credential(
        self,
        actor_id: str,
        old_credential_id: str,
        new_credential_id: str,
        identity_confirmed: bool,
        now: datetime | None = None,
    ) -> Credential:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "replace_credential", current_time)
        old = self.access.credentials.get(old_credential_id)
        if old is None:
            raise ValueError("Credential is not registered.")
        if old.status != CredentialStatus.REVOKED:
            old.status = CredentialStatus.REVOKED
            self._record(
                "credential_revoked",
                {"credential_id": old_credential_id, "reason": "REPLACED"},
                "security",
                current_time,
            )
        replacement = Credential(
            credential_id=new_credential_id,
            user_id=old.user_id,
            role=old.role,
            allowed_floors=set(old.allowed_floors) if identity_confirmed else set(),
        )
        self.access.register(replacement)
        self._record(
            "credential_replaced",
            {
                "old_credential_id": old_credential_id,
                "new_credential_id": new_credential_id,
                "identity_confirmed": identity_confirmed,
            },
            "security",
            current_time,
        )
        self._audit(
            actor_id,
            "replace_credential",
            "SUCCESS",
            {
                "old_credential_id": old_credential_id,
                "new_credential_id": new_credential_id,
                "identity_confirmed": identity_confirmed,
            },
            current_time,
        )
        return replacement

    def request_hall_call(
        self,
        floor: int,
        direction: Direction,
        freight: bool = False,
        now: datetime | None = None,
    ) -> HallRequest:
        current_time = now or utc_now()
        if not self.config.validate_floor(floor):
            raise ValueError("Hall-call floor is outside the configured range.")
        if not isinstance(direction, Direction) or direction == Direction.IDLE:
            raise ValueError("Hall calls require UP or DOWN direction.")
        request_type = RequestType.FREIGHT if freight else RequestType.HALL
        if freight and floor == -1:
            raise ValueError("E5 does not serve B1.")
        if not freight:
            if (floor == self.config.maximum_floor and direction == Direction.UP) or (
                floor == self.config.minimum_floor and direction == Direction.DOWN
            ):
                raise ValueError("No elevator service exists in that direction at this floor.")
        for existing in self.hall_requests.values():
            if (
                existing.floor == floor
                and existing.direction == direction
                and existing.request_type == request_type
                and existing.status
                in {
                    RequestStatus.RECEIVED,
                    RequestStatus.QUEUED,
                    RequestStatus.ASSIGNED,
                    RequestStatus.APPROACHING,
                }
            ):
                self._record(
                    "hall_call_merged",
                    {"request_id": existing.request_id, "floor": floor},
                    at=current_time,
                )
                return existing
        active = sum(
            request.status
            in {
                RequestStatus.RECEIVED,
                RequestStatus.QUEUED,
                RequestStatus.ASSIGNED,
                RequestStatus.APPROACHING,
                RequestStatus.BLOCKED,
            }
            for request in self.hall_requests.values()
        )
        request = HallRequest(
            floor=floor,
            request_type=request_type,
            direction=direction,
            accepted_at=current_time,
        )
        self.hall_requests[request.request_id] = request
        self._record(
            "hall_call_received",
            {
                "request_id": request.request_id,
                "origin_floor": floor,
                "call_type": request_type.value,
                "direction": direction.value,
                "accepted_at": current_time.isoformat(),
            },
            at=current_time,
        )
        if active >= self.config.maximum_active_requests:
            request.status = RequestStatus.QUEUED
            request.reason = "Active request backlog limit reached; request retained."
            self._record(
                "hall_call_backlog_limit",
                {"request_id": request.request_id, "active_requests": active + 1},
                at=current_time,
            )
            return request
        if freight and not is_eligible(
            self.states["E5"], RequestType.FREIGHT, floor, self.config
        ):
            request.status = RequestStatus.REJECTED
            request.reason = "E5 is occupied or otherwise unavailable."
            self._record(
                "freight_call_rejected",
                {"request_id": request.request_id, "reason": request.reason},
                at=current_time,
            )
            return request
        self._assign_request(request, current_time)
        return request

    def _set_hall_status(
        self,
        request: HallRequest,
        status: RequestStatus,
        at: datetime,
        reason: str | None = None,
    ) -> None:
        request.status = status
        request.reason = reason
        request.event_history.append(
            {"status": status.value, "timestamp": at.isoformat(), "reason": reason}
        )
        self._record(
            "hall_request_state",
            {
                "request_id": request.request_id,
                "status": status.value,
                "reason": reason,
            },
            at=at,
        )

    def _assign_request(self, request: HallRequest, at: datetime) -> None:
        elevator_id = choose_elevator(
            self.states, request.floor, request.request_type, self.config
        )
        if elevator_id is None:
            self._set_hall_status(
                request, RequestStatus.QUEUED, at, "No eligible elevator is currently available."
            )
            return
        try:
            self.interface.send_command(
                elevator_id,
                "assign_hall_call",
                request_id=request.request_id,
                floor=request.floor,
                direction=request.direction.value if request.direction else None,
                request_type=request.request_type.value,
            )
        except InterfaceUnavailable as exc:
            request.assigned_elevator = None
            self._set_hall_status(request, RequestStatus.QUEUED, at, str(exc))
            return
        request.assigned_elevator = elevator_id
        self.states[elevator_id].pending_stops.append(request.floor)
        self._set_hall_status(request, RequestStatus.ASSIGNED, at)
        self._record(
            "elevator_assigned",
            {"request_id": request.request_id, "elevator_id": elevator_id},
            at=at,
        )

    def _requeue_for_car(self, elevator_id: str, at: datetime) -> None:
        for request in self.hall_requests.values():
            if (
                request.assigned_elevator == elevator_id
                and request.status
                in {RequestStatus.ASSIGNED, RequestStatus.APPROACHING}
            ):
                request.assigned_elevator = None
                self._set_hall_status(
                    request,
                    RequestStatus.QUEUED,
                    at,
                    "Assigned elevator became unavailable; original acceptance time retained.",
                )
                self._assign_request(request, at)
        for request in self.car_requests.values():
            if request.elevator_id == elevator_id and request.status in {
                RequestStatus.QUEUED,
                RequestStatus.IN_TRANSIT,
                RequestStatus.ASSIGNED,
            }:
                request.status = RequestStatus.BLOCKED
                request.reason = "Elevator became unavailable; reconciliation required."
                self._record(
                    "car_request_blocked",
                    {"request_id": request.request_id, "elevator_id": elevator_id},
                    at=at,
                )

    def _trim_window(
        self, values: deque[Any], now: datetime, seconds: int
    ) -> None:
        cutoff = now - timedelta(seconds=seconds)
        while values:
            oldest = values[0][0] if isinstance(values[0], tuple) else values[0]
            if oldest >= cutoff:
                break
            values.popleft()

    def _record_failed_auth(
        self,
        credential_id: str,
        elevator_id: str,
        destination: int,
        reason: str,
        now: datetime,
        immediate_alert: bool = False,
    ) -> None:
        self._audit(
            credential_id,
            "destination_authorization",
            "DENIED",
            {
                "credential_id": credential_id,
                "elevator_id": elevator_id,
                "destination": destination,
                "reason": reason,
            },
            now,
        )
        self._record(
            "destination_denied",
            {
                "credential_id": credential_id,
                "elevator_id": elevator_id,
                "destination": destination,
                "reason": reason,
            },
            "security",
            now,
        )
        if immediate_alert:
            self._alert(
                "inactive_credential_use",
                "HIGH",
                {
                    "credential_id": credential_id,
                    "elevator_id": elevator_id,
                    "destination": destination,
                    "reason": reason,
                },
                now,
            )
        failures = self._failed_auth[credential_id]
        self._trim_window(failures, now, self.config.failed_authentication_window_seconds)
        failures.append(now)
        if (
            len(failures) >= self.config.failed_authentication_threshold
            or immediate_alert
        ):
            if not immediate_alert:
                self._alert(
                    "repeated_authorization_failure",
                    "MEDIUM",
                    {
                        "credential_id": credential_id,
                        "elevator_id": elevator_id,
                        "attempts": len(failures),
                    },
                    now,
                )

    def _check_cloned_credential(
        self, credential_id: str, elevator_id: str, now: datetime
    ) -> bool:
        presentations = self._presentations[credential_id]
        self._trim_window(presentations, now, self.config.cloned_card_window_seconds)
        previous_cars = {car for _, car in presentations if car != elevator_id}
        if previous_cars:
            credential = self.access.credentials.get(credential_id)
            if credential and credential.status == CredentialStatus.ACTIVE:
                credential.status = CredentialStatus.SUSPENDED
                credential.suspended_reason = "CLONE_SUSPECTED"
                credential.suspended_at = now
            self._record(
                "cloned_credential_detected",
                {
                    "credential_id": credential_id,
                    "cars": sorted(previous_cars | {elevator_id}),
                },
                "security",
                now,
            )
            self._audit(
                "SYSTEM",
                "cloned_credential_detected",
                "DENIED",
                {
                    "credential_id": credential_id,
                    "cars": sorted(previous_cars | {elevator_id}),
                },
                now,
            )
            self._alert(
                "cloned_credential",
                "HIGH",
                {
                    "credential_id": credential_id,
                    "cars": sorted(previous_cars | {elevator_id}),
                },
                now,
            )
            return True
        presentations.append((now, elevator_id))
        return False

    def _track_unknown_card(self, credential_id: str, now: datetime) -> None:
        self._trim_window(
            self._unknown_cards, now, self.config.unregistered_card_window_seconds
        )
        self._unknown_cards.append((now, credential_id))
        identifiers = {item for _, item in self._unknown_cards}
        if len(identifiers) > self.config.unregistered_card_threshold:
            if not any(
                alert["event_type"] == "suspected_card_scanning"
                and alert["timestamp"]
                >= (now - timedelta(seconds=self.config.unregistered_card_window_seconds)).isoformat()
                for alert in self.alerts
            ):
                self._alert(
                    "suspected_card_scanning",
                    "HIGH",
                    {"distinct_credentials": sorted(identifiers)},
                    now,
                )

    def submit_car_call(
        self,
        elevator_id: str,
        destination: int,
        credential_id: str | None = None,
        now: datetime | None = None,
    ) -> CarRequest:
        current_time = now or utc_now()
        self.expire_credentials(current_time)
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        if not self.config.validate_floor(destination):
            raise ValueError("Destination is outside the configured floor range.")
        car = next(item for item in self.config.elevators if item.elevator_id == elevator_id)
        request = CarRequest(
            elevator_id=elevator_id,
            destination=destination,
            credential_id=credential_id,
            accepted_at=current_time,
        )
        self.car_requests[request.request_id] = request
        request.status = RequestStatus.AUTHORIZATION_CHECK
        if destination == -1 and not car.serves_b1:
            request.status = RequestStatus.REJECTED
            request.reason = "This elevator does not serve B1."
            self._audit(
                credential_id,
                "destination_authorization",
                "DENIED",
                {
                    "request_id": request.request_id,
                    "elevator_id": elevator_id,
                    "destination": destination,
                    "reason": request.reason,
                },
                current_time,
            )
            self._record(
                "destination_denied",
                {"request_id": request.request_id, "reason": request.reason},
                "security",
                current_time,
            )
            return request
        if destination != -1 and credential_id:
            if self._check_cloned_credential(credential_id, elevator_id, current_time):
                request.status = RequestStatus.REJECTED
                request.reason = "Credential presentation is suspected to be cloned."
                return request
        allowed, reason, credential = self.access.authorize_destination(
            credential_id, destination, current_time
        )
        if not allowed:
            request.status = RequestStatus.REJECTED
            request.reason = reason
            inactive = credential is not None and credential.status != CredentialStatus.ACTIVE
            self._record_failed_auth(
                credential_id or "UNREGISTERED",
                elevator_id,
                destination,
                reason,
                current_time,
                immediate_alert=inactive,
            )
            if credential is None:
                self._track_unknown_card(credential_id or "UNREGISTERED", current_time)
            return request
        # A restricted destination is fail-closed if the authorization audit cannot be stored.
        self._audit(
            credential_id,
            "destination_authorization",
            "ALLOWED",
            {
                "request_id": request.request_id,
                "elevator_id": elevator_id,
                "destination": destination,
            },
            current_time,
        )
        state = self.states[elevator_id]
        if state.stale or not self.interface.connected:
            request.status = RequestStatus.BLOCKED
            request.reason = "Elevator state is stale or the interface is disconnected."
            self._record(
                "destination_blocked",
                {"request_id": request.request_id, "reason": request.reason},
                at=current_time,
            )
            return request
        if state.availability != Availability.IN_SERVICE:
            request.status = RequestStatus.REJECTED
            request.reason = "Elevator is not available for a new car call."
            self._record(
                "destination_rejected",
                {"request_id": request.request_id, "reason": request.reason},
                at=current_time,
            )
            return request
        try:
            self.interface.send_command(
                elevator_id,
                "select_destination",
                request_id=request.request_id,
                destination=destination,
            )
        except InterfaceUnavailable as exc:
            request.status = RequestStatus.BLOCKED
            request.reason = str(exc)
            self._record(
                "destination_blocked",
                {"request_id": request.request_id, "reason": request.reason},
                at=current_time,
            )
            return request
        request.status = RequestStatus.QUEUED
        state.pending_stops.append(destination)
        self._record(
            "car_call_queued",
            {
                "request_id": request.request_id,
                "elevator_id": elevator_id,
                "destination": destination,
                "credential_id": credential_id,
            },
            at=current_time,
        )
        return request

    def mark_boarding_evidence(self, elevator_id: str, now: datetime | None = None) -> None:
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        for request_id, (arrived_at, car_id, evidence) in list(
            self._arrived_hall_requests.items()
        ):
            if car_id == elevator_id and not evidence:
                self._arrived_hall_requests[request_id] = (arrived_at, car_id, True)
        self._record(
            "boarding_evidence",
            {"elevator_id": elevator_id},
            at=now or utc_now(),
        )

    def update_elevator_state(
        self, incoming: ElevatorState, now: datetime | None = None
    ) -> bool:
        current_time = now or incoming.updated_at or utc_now()
        existing = self.states.get(incoming.elevator_id)
        reason = None
        if existing is None:
            reason = "Unknown elevator identifier."
        elif not self.config.validate_floor(incoming.floor):
            reason = "Floor is outside the configured range."
        elif not isinstance(incoming.direction, Direction) or not isinstance(
            incoming.door, DoorState
        ):
            reason = "Unknown direction or door state."
        elif incoming.updated_at < existing.updated_at:
            reason = "State timestamp is older than the last valid update."
        elif existing.emergency and incoming.emergency is None and (
            incoming.availability == Availability.IN_SERVICE
        ):
            reason = "Emergency recovery requires an authorized resolution transition."
        if reason:
            details = {
                "elevator_id": incoming.elevator_id,
                "reason": reason,
                "reported_floor": incoming.floor,
            }
            self._record("external_message_rejected", details, "security", current_time)
            self._audit("INTERFACE", "validate_elevator_state", "REJECTED", details, current_time)
            return False
        previous_floor = existing.floor
        incoming.stale = not self.interface.connected
        self.states[incoming.elevator_id] = incoming
        if isinstance(self.interface, SimulatedElevatorInterface):
            self.interface.publish_state(incoming)
        self._record(
            "elevator_state_updated",
            {
                "elevator_id": incoming.elevator_id,
                "floor": incoming.floor,
                "direction": incoming.direction.value,
                "door": incoming.door.value,
                "availability": incoming.availability.value,
                "stale": incoming.stale,
            },
            at=current_time,
        )
        if previous_floor != incoming.floor:
            self._record(
                "floor_changed",
                {
                    "elevator_id": incoming.elevator_id,
                    "floor": incoming.floor,
                    "direction": incoming.direction.value,
                },
                category="accessibility",
                at=current_time,
            )
        self._complete_arrivals(incoming, current_time)
        for request in self.hall_requests.values():
            if (
                request.assigned_elevator == incoming.elevator_id
                and request.status == RequestStatus.ASSIGNED
            ):
                self._set_hall_status(request, RequestStatus.APPROACHING, current_time)
        return True

    def _complete_arrivals(self, state: ElevatorState, at: datetime) -> None:
        for request in self.hall_requests.values():
            if (
                request.assigned_elevator == state.elevator_id
                and request.status in {RequestStatus.ASSIGNED, RequestStatus.APPROACHING}
                and request.floor == state.floor
                and state.door == DoorState.OPEN
            ):
                request.arrival_at = state.updated_at
                request.completed_at = state.updated_at
                self._set_hall_status(request, RequestStatus.ARRIVED, at)
                self._set_hall_status(request, RequestStatus.COMPLETED, at)
                self._arrived_hall_requests[request.request_id] = (
                    state.updated_at,
                    state.elevator_id,
                    False,
                )
                if request.floor in state.pending_stops:
                    state.pending_stops.remove(request.floor)
        for request in self.car_requests.values():
            if (
                request.elevator_id == state.elevator_id
                and request.destination == state.floor
                and request.status in {RequestStatus.QUEUED, RequestStatus.IN_TRANSIT}
            ):
                request.status = RequestStatus.COMPLETED
                request.arrival_at = state.updated_at
                self._record(
                    "car_call_completed",
                    {
                        "request_id": request.request_id,
                        "elevator_id": state.elevator_id,
                        "destination": state.floor,
                    },
                    at=at,
                )
                if request.destination in state.pending_stops:
                    state.pending_stops.remove(request.destination)

    def evaluate_empty_calls(self, now: datetime | None = None) -> list[str]:
        current_time = now or utc_now()
        flagged = []
        for request_id, (arrived_at, car_id, evidence) in list(
            self._arrived_hall_requests.items()
        ):
            if evidence or request_id not in self.hall_requests:
                continue
            if (
                current_time - arrived_at
                < timedelta(seconds=self.config.empty_call_window_seconds)
            ):
                continue
            request = self.hall_requests[request_id]
            if "NO_BOARDING" in request.flags:
                continue
            request.flags.add("NO_BOARDING")
            self._arrived_hall_requests[request_id] = (arrived_at, car_id, True)
            self._no_boarding_events[request.floor].append(current_time)
            self._trim_window(
                self._no_boarding_events[request.floor],
                current_time,
                self.config.no_boarding_window_seconds,
            )
            flagged.append(request_id)
            self._record(
                "hall_call_no_boarding",
                {
                    "request_id": request_id,
                    "floor": request.floor,
                    "wait_seconds": (arrived_at - request.accepted_at).total_seconds(),
                },
                "security",
                current_time,
            )
            if (
                len(self._no_boarding_events[request.floor])
                >= self.config.no_boarding_threshold
            ):
                self._alert(
                    "repeated_empty_hall_calls",
                    "LOW",
                    {
                        "floor": request.floor,
                        "request_ids": [
                            item.request_id
                            for item in self.hall_requests.values()
                            if item.floor == request.floor
                            and "NO_BOARDING" in item.flags
                        ],
                    },
                    current_time,
                )
        return flagged

    def _new_incident(
        self,
        event_type: str,
        elevator_id: str | None,
        source: str,
        at: datetime,
    ) -> Incident:
        state = self.states.get(elevator_id or "")
        incident = Incident(
            incident_id=str(uuid4()),
            event_type=event_type,
            scope="BUILDING" if elevator_id is None else "ELEVATOR",
            elevator_id=elevator_id,
            floor=state.floor if state else None,
            direction=state.direction if state else None,
            source=source,
            created_at=at,
            history=[{"status": IncidentStatus.OPEN.value, "timestamp": at.isoformat()}],
        )
        self.incidents[incident.incident_id] = incident
        self._record(
            "incident_created",
            {
                "incident_id": incident.incident_id,
                "event_type": event_type,
                "scope": incident.scope,
                "elevator_id": elevator_id,
                "floor": incident.floor,
                "direction": incident.direction.value if incident.direction else None,
                "source": source,
            },
            "emergency",
            at,
        )
        self._alert(
            event_type,
            "CRITICAL" if event_type in {"MEDICAL", "FIRE", "POWER_FAILURE", "STUCK"} else "HIGH",
            {
                "incident_id": incident.incident_id,
                "elevator_id": elevator_id,
                "floor": incident.floor,
                "direction": incident.direction.value if incident.direction else None,
                "source": source,
            },
            at,
        )
        return incident

    def trigger_emergency(
        self,
        event_type: str,
        elevator_id: str | None,
        source: str = "PASSENGER",
        now: datetime | None = None,
    ) -> Incident:
        current_time = now or utc_now()
        normalized_type = event_type.upper()
        allowed = {
            "HELP",
            "MEDICAL",
            "STUCK",
            "FIRE",
            "POWER_FAILURE",
            "DOOR_OBSTRUCTION",
            "OVERLOAD",
        }
        if normalized_type not in allowed:
            raise ValueError(f"Emergency mode {event_type!r} is not implemented.")
        if normalized_type not in {"FIRE", "POWER_FAILURE"} and elevator_id not in self.states:
            raise ValueError("A known elevator is required for this emergency.")
        if normalized_type in {"FIRE", "POWER_FAILURE"}:
            scope_ids = list(self.states)
            self._building_emergency.add(normalized_type)
        elif elevator_id:
            scope_ids = [elevator_id]
        else:
            scope_ids = []
        for car_id in scope_ids:
            self._emergency_triggers[car_id].append(current_time)
            self._trim_window(
                self._emergency_triggers[car_id],
                current_time,
                self.config.repeat_emergency_window_seconds,
            )
        incident = self._new_incident(
            normalized_type, elevator_id, source, current_time
        )
        self._audit(
            source,
            "trigger_emergency",
            "SUCCESS",
            {
                "incident_id": incident.incident_id,
                "event_type": normalized_type,
                "elevator_id": elevator_id,
            },
            current_time,
        )
        if normalized_type in {"FIRE", "POWER_FAILURE", "MEDICAL", "STUCK"}:
            for car_id in scope_ids:
                state = self.states[car_id]
                current_type = state.emergency
                if (
                    current_type is None
                    or self._EMERGENCY_PRIORITY[normalized_type]
                    > self._EMERGENCY_PRIORITY.get(current_type, 0)
                ):
                    state.availability = Availability.EMERGENCY
                    state.emergency = normalized_type
            for car_id in scope_ids:
                state = self.states[car_id]
                if normalized_type == "MEDICAL":
                    self._medical_cars.add(car_id)
            for car_id in scope_ids:
                self._requeue_for_car(car_id, current_time)
            if (
                normalized_type == "MEDICAL"
                and elevator_id
                and not (self._building_emergency & {"FIRE", "POWER_FAILURE"})
                and self.states[elevator_id].emergency != "STUCK"
            ):
                try:
                    self.interface.send_command(
                        elevator_id,
                        "medical_responder_run",
                        destination=self.config.responder_floor,
                        non_stop=True,
                        incident_id=incident.incident_id,
                    )
                except InterfaceUnavailable:
                    self._record(
                        "medical_responder_run_deferred",
                        {"incident_id": incident.incident_id, "reason": "Interface disconnected."},
                        "emergency",
                        current_time,
                    )
        if elevator_id:
            triggers = self._emergency_triggers[elevator_id]
            if len(triggers) >= self.config.repeat_emergency_threshold:
                self._alert(
                    "repeated_emergency_triggers",
                    "LOW",
                    {"elevator_id": elevator_id, "trigger_count": len(triggers)},
                    current_time,
                )
        return incident

    def confirm_help_request(
        self,
        actor_id: str,
        incident_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "confirm_help_request", current_time)
        incident = self.incidents.get(incident_id)
        if incident is None or incident.event_type != "HELP" or not incident.elevator_id:
            raise ValueError("An open car-specific HELP incident is required.")
        state = self.states[incident.elevator_id]
        state.availability = Availability.EMERGENCY
        state.emergency = "STUCK"
        incident.event_type = "STUCK"
        self._requeue_for_car(incident.elevator_id, current_time)
        self._record(
            "help_confirmed",
            {"incident_id": incident_id, "elevator_id": incident.elevator_id},
            "emergency",
            current_time,
        )
        self._audit(
            actor_id,
            "confirm_help_request",
            "SUCCESS",
            {"incident_id": incident_id},
            current_time,
        )

    def dispatch_rescue_car(
        self,
        actor_id: str,
        affected_elevator_id: str,
        floor: int,
        occupancy_confirmed_empty: bool,
        wait_seconds: int = 0,
        now: datetime | None = None,
    ) -> str:
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "dispatch_rescue_car", current_time)
        if affected_elevator_id not in self.states or not self.config.validate_floor(floor):
            raise ValueError("A known affected elevator and valid rescue floor are required.")
        if not occupancy_confirmed_empty:
            raise ValueError("Admin must confirm the rescue car is empty.")
        if wait_seconds < 0 or wait_seconds > 600:
            raise ValueError("Rescue wait must be between 0 and 600 seconds.")
        candidates = {
            key: value
            for key, value in self.states.items()
            if key != affected_elevator_id and key != "E5"
        }
        rescue_id = choose_elevator(
            candidates, floor, RequestType.HALL, self.config
        )
        if rescue_id is None:
            self._audit(
                actor_id,
                "dispatch_rescue_car",
                "REJECTED",
                {"affected_elevator_id": affected_elevator_id, "floor": floor},
                current_time,
            )
            raise RuntimeError("No eligible empty residential rescue car is available.")
        self._requeue_for_car(rescue_id, current_time)
        self._audit(
            actor_id,
            "dispatch_rescue_car",
            "SUCCESS",
            {
                "affected_elevator_id": affected_elevator_id,
                "rescue_elevator_id": rescue_id,
                "floor": floor,
                "wait_seconds": wait_seconds,
                "occupancy_confirmed_empty": occupancy_confirmed_empty,
            },
            current_time,
        )
        try:
            self.interface.send_command(
                rescue_id,
                "rescue_dispatch",
                destination=floor,
                affected_elevator_id=affected_elevator_id,
                wait_seconds=wait_seconds,
            )
        except InterfaceUnavailable as exc:
            self._audit(
                actor_id,
                "dispatch_rescue_car",
                "FAILED",
                {"rescue_elevator_id": rescue_id, "reason": str(exc)},
                current_time,
            )
            raise
        self._record(
            "rescue_car_dispatched",
            {
                "rescue_elevator_id": rescue_id,
                "affected_elevator_id": affected_elevator_id,
                "floor": floor,
                "wait_seconds": wait_seconds,
            },
            "emergency",
            current_time,
        )
        return rescue_id

    def set_maintenance(
        self,
        actor_id: str,
        elevator_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(
            actor_id, {Role.ADMIN, Role.TECHNICIAN}, "set_maintenance", current_time
        )
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        self.states[elevator_id].availability = Availability.MAINTENANCE
        self._requeue_for_car(elevator_id, current_time)
        self._record(
            "maintenance_started",
            {"elevator_id": elevator_id, "actor_id": actor_id, "reason": reason},
            "maintenance",
            current_time,
        )
        self._audit(
            actor_id,
            "set_maintenance",
            "SUCCESS",
            {"elevator_id": elevator_id, "reason": reason},
            current_time,
        )

    def report_fault(
        self,
        elevator_id: str,
        fault: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        state = self.states[elevator_id]
        state.fault = fault
        state.availability = Availability.FAULT
        self._requeue_for_car(elevator_id, current_time)
        self._record(
            "fault_reported",
            {"elevator_id": elevator_id, "fault": fault},
            "maintenance",
            current_time,
        )
        self._alert(
            "elevator_fault",
            "HIGH",
            {"elevator_id": elevator_id, "fault": fault},
            current_time,
        )

    def return_to_service(
        self,
        actor_id: str,
        elevator_id: str,
        recovery_confirmed: bool,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        self._actor(
            actor_id, {Role.ADMIN, Role.TECHNICIAN}, "return_to_service", current_time
        )
        state = self.states.get(elevator_id)
        if state is None:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        if (
            not recovery_confirmed
            or state.stale
            or not self.interface.connected
            or state.fault is not None
            or state.door == DoorState.OBSTRUCTED
        ):
            self._audit(
                actor_id,
                "return_to_service",
                "REJECTED",
                {"elevator_id": elevator_id, "reason": "Recovery checks failed."},
                current_time,
            )
            raise ValueError("Fresh state, no active fault/obstruction, and confirmation are required.")
        state.availability = Availability.IN_SERVICE
        state.emergency = None
        self._medical_cars.discard(elevator_id)
        self._record(
            "maintenance_ended",
            {"elevator_id": elevator_id, "actor_id": actor_id},
            "maintenance",
            current_time,
        )
        self._audit(
            actor_id,
            "return_to_service",
            "SUCCESS",
            {"elevator_id": elevator_id},
            current_time,
        )
        self._dispatch_queued(current_time)

    def _dispatch_queued(self, now: datetime) -> None:
        for request in sorted(
            self.hall_requests.values(), key=lambda item: item.accepted_at
        ):
            if request.status == RequestStatus.QUEUED:
                self._assign_request(request, now)

    def control_door(
        self,
        elevator_id: str,
        command: str,
        actor_id: str = "PASSENGER",
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utc_now()
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        state = self.states[elevator_id]
        normalized = command.lower()
        if normalized not in {"open", "close"}:
            raise ValueError("Door command must be 'open' or 'close'.")
        reason = None
        if not self.interface.connected or state.stale:
            reason = "Elevator state is stale or the interface is disconnected."
        elif normalized == "close" and (
            state.door == DoorState.OBSTRUCTED or elevator_id in self._obstructions
        ):
            reason = "Door close is inhibited while obstruction is active."
        elif normalized == "open" and not self.config.validate_floor(state.floor):
            reason = "Door open requires a valid current floor."
        if reason:
            self._audit(
                actor_id,
                f"door_{normalized}",
                "REJECTED",
                {"elevator_id": elevator_id, "reason": reason},
                current_time,
            )
            self._record(
                f"door_{normalized}_rejected",
                {"elevator_id": elevator_id, "reason": reason},
                at=current_time,
            )
            return False
        self.interface.send_command(elevator_id, f"door_{normalized}")
        self._record(
            f"door_{normalized}_commanded", {"elevator_id": elevator_id}, at=current_time
        )
        return True

    def set_obstruction(
        self,
        elevator_id: str,
        active: bool,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        if elevator_id not in self.states:
            raise ValueError(f"Unknown elevator {elevator_id!r}.")
        if active:
            self._obstructions.setdefault(elevator_id, current_time)
            self.states[elevator_id].door = DoorState.OBSTRUCTED
            self._record(
                "door_obstruction_started", {"elevator_id": elevator_id}, at=current_time
            )
            self.check_obstruction_escalations(current_time)
        else:
            self._obstructions.pop(elevator_id, None)
            self._obstruction_alerted.discard(elevator_id)
            if self.states[elevator_id].door == DoorState.OBSTRUCTED:
                self.states[elevator_id].door = DoorState.OPEN
            self._record(
                "door_obstruction_cleared", {"elevator_id": elevator_id}, at=current_time
            )

    def check_obstruction_escalations(
        self, now: datetime | None = None
    ) -> list[str]:
        current_time = now or utc_now()
        escalated = []
        for elevator_id, started_at in self._obstructions.items():
            if (
                elevator_id not in self._obstruction_alerted
                and current_time - started_at
                >= timedelta(seconds=self.config.obstruction_warning_seconds)
            ):
                self._obstruction_alerted.add(elevator_id)
                incident = self._new_incident(
                    "DOOR_OBSTRUCTION", elevator_id, "ELEVATOR_INTERFACE", current_time
                )
                self._audit(
                    "SYSTEM",
                    "door_obstruction_escalated",
                    "SUCCESS",
                    {"elevator_id": elevator_id, "incident_id": incident.incident_id},
                    current_time,
                )
                escalated.append(elevator_id)
        return escalated

    def start_service_run(
        self,
        actor_id: str,
        destination: int,
        now: datetime | None = None,
    ) -> str:
        current_time = now or utc_now()
        actor = self._actor(
            actor_id,
            {Role.ADMIN, Role.TECHNICIAN, Role.BUILDING_SERVICE},
            "start_service_run",
            current_time,
        )
        if not self.config.validate_floor(destination) or destination == -1:
            raise ValueError("E5 service destination must be a served floor other than B1.")
        state = self.states["E5"]
        if not is_eligible(state, RequestType.FREIGHT, destination, self.config):
            raise ValueError("E5 is occupied or otherwise unavailable.")
        request = HallRequest(
            floor=destination,
            request_type=RequestType.SERVICE,
            direction=Direction.IDLE,
            status=RequestStatus.ASSIGNED,
            assigned_elevator="E5",
            accepted_at=current_time,
        )
        self.hall_requests[request.request_id] = request
        self._audit(
            actor_id,
            "start_service_run",
            "SUCCESS",
            {
                "elevator_id": "E5",
                "destination": destination,
                "request_id": request.request_id,
                "role": actor.role.value if actor else None,
            },
            current_time,
        )
        try:
            self.interface.send_command(
                "E5",
                "service_run",
                request_id=request.request_id,
                destination=destination,
                non_stop=True,
            )
        except InterfaceUnavailable:
            request.status = RequestStatus.FAILED
            request.reason = "Elevator interface is disconnected."
            raise
        state.availability = Availability.OCCUPIED
        state.pending_stops = [destination]
        self._record(
            "service_run_started",
            {"request_id": request.request_id, "destination": destination},
            at=current_time,
        )
        return request.request_id

    def complete_service_run(
        self, request_id: str, now: datetime | None = None
    ) -> None:
        current_time = now or utc_now()
        request = self.hall_requests.get(request_id)
        if request is None or request.request_type != RequestType.SERVICE:
            raise ValueError("Unknown E5 service run.")
        request.status = RequestStatus.COMPLETED
        request.completed_at = current_time
        self.states["E5"].availability = Availability.IN_SERVICE
        self.states["E5"].pending_stops.clear()
        self._record("service_run_completed", {"request_id": request_id}, at=current_time)
        self._dispatch_queued(current_time)

    def lose_communication(
        self,
        elevator_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        affected = list(self.states) if elevator_id is None else [elevator_id]
        if any(item not in self.states for item in affected):
            raise ValueError("Communication loss references an unknown elevator.")
        if isinstance(self.interface, SimulatedElevatorInterface):
            self.interface.disconnect()
        for car_id in affected:
            self.states[car_id].stale = True
            self.states[car_id].availability = Availability.STALE
        self._record(
            "communication_lost",
            {"elevators": affected},
            "maintenance",
            current_time,
        )
        self._new_incident("COMMUNICATION_FAILURE", None, "ELEVATOR_INTERFACE", current_time)
        for car_id in affected:
            self._requeue_for_car(car_id, current_time)

    def restore_communication(
        self, now: datetime | None = None
    ) -> dict[str, bool]:
        current_time = now or utc_now()
        if isinstance(self.interface, SimulatedElevatorInterface):
            self.interface.reconnect()
        snapshot = self.interface.request_snapshot()
        outcomes: dict[str, bool] = {}
        for elevator_id, state in snapshot.items():
            if elevator_id not in self.states:
                self._record(
                    "external_message_rejected",
                    {"elevator_id": elevator_id, "reason": "Unknown snapshot elevator."},
                    "security",
                    current_time,
                )
                continue
            state.stale = False
            state.availability = (
                Availability.IN_SERVICE
                if self.states[elevator_id].availability == Availability.STALE
                else self.states[elevator_id].availability
            )
            outcomes[elevator_id] = self.update_elevator_state(state, current_time)
        missing = set(self.states) - set(outcomes)
        for elevator_id in missing:
            self.states[elevator_id].stale = True
            self.states[elevator_id].availability = Availability.STALE
            outcomes[elevator_id] = False
        self._record(
            "communication_restored",
            {"reconciled": outcomes},
            "maintenance",
            current_time,
        )
        if all(outcomes.values()):
            for state in self.states.values():
                if state.availability == Availability.STALE:
                    state.availability = Availability.IN_SERVICE
                    state.stale = False
            self._dispatch_queued(current_time)
        return outcomes

    def resolve_incident(
        self,
        actor_id: str,
        incident_id: str,
        false_alarm: bool = False,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        incident = self.incidents.get(incident_id)
        if incident is None or incident.status in {
            IncidentStatus.RESOLVED,
            IncidentStatus.FALSE_ALARM,
        }:
            raise ValueError("An open incident is required.")
        roles = (
            {Role.ADMIN}
            if incident.event_type in {"MEDICAL", "FIRE", "POWER_FAILURE", "STUCK"}
            else {Role.ADMIN, Role.TECHNICIAN}
        )
        self._actor(actor_id, roles, "resolve_incident", current_time)
        if incident.elevator_id:
            state = self.states[incident.elevator_id]
            if (
                not false_alarm
                and (state.stale or state.fault or state.door == DoorState.OBSTRUCTED)
            ):
                raise ValueError("Incident cannot be resolved until recovery checks pass.")
            if state.emergency == incident.event_type or (
                incident.event_type == "STUCK" and state.emergency == "STUCK"
            ):
                state.emergency = None
                state.availability = Availability.IN_SERVICE
                self._medical_cars.discard(incident.elevator_id)
        incident.status = (
            IncidentStatus.FALSE_ALARM if false_alarm else IncidentStatus.RESOLVED
        )
        incident.history.append(
            {"status": incident.status.value, "timestamp": current_time.isoformat()}
        )
        self._record(
            "incident_resolved",
            {
                "incident_id": incident_id,
                "status": incident.status.value,
                "actor_id": actor_id,
            },
            "emergency",
            current_time,
        )
        self._audit(
            actor_id,
            "resolve_incident",
            "SUCCESS",
            {"incident_id": incident_id, "status": incident.status.value},
            current_time,
        )
        self._dispatch_queued(current_time)

    def dashboard_sign_in(
        self, credential_id: str, now: datetime | None = None
    ) -> bool:
        current_time = now or utc_now()
        self.expire_credentials(current_time)
        if not self.access.has_role(credential_id, {Role.ADMIN}, current_time):
            self._audit(
                credential_id,
                "dashboard_sign_in",
                "DENIED",
                {"reason": "Active Admin credential required."},
                current_time,
            )
            return False
        self._dashboard_activity[credential_id] = current_time
        self._audit(credential_id, "dashboard_sign_in", "SUCCESS", {}, current_time)
        return True

    def dashboard_view(
        self, credential_id: str, now: datetime | None = None
    ) -> dict[str, Any]:
        current_time = now or utc_now()
        self.expire_credentials(current_time)
        last_activity = self._dashboard_activity.get(credential_id)
        if (
            last_activity is None
            or current_time - last_activity
            > timedelta(seconds=self.config.dashboard_session_timeout_seconds)
            or not self.access.has_role(credential_id, {Role.ADMIN}, current_time)
        ):
            self._dashboard_activity.pop(credential_id, None)
            raise PermissionError("Dashboard session is missing, expired, or unauthorized.")
        self._dashboard_activity[credential_id] = current_time
        return {
            "fleet": {
                key: {
                    "floor": state.floor,
                    "direction": state.direction.value,
                    "door": state.door.value,
                    "availability": state.availability.value,
                    "fault": state.fault,
                    "emergency": state.emergency,
                    "stale": state.stale,
                }
                for key, state in self.states.items()
            },
            "active_requests": [
                request
                for request in self.hall_requests.values()
                if request.status
                in {
                    RequestStatus.QUEUED,
                    RequestStatus.ASSIGNED,
                    RequestStatus.APPROACHING,
                    RequestStatus.BLOCKED,
                }
            ],
            "incidents": list(self.incidents.values()),
            "alerts": list(self.alerts),
        }

    def audit_integrity(self, actor_id: str, now: datetime | None = None):
        current_time = now or utc_now()
        self._actor(actor_id, {Role.ADMIN}, "audit_integrity", current_time)
        result = self.audit.verify()
        self._audit(
            actor_id,
            "audit_integrity",
            "VALID" if result.valid else "TAMPERED",
            {
                "first_invalid_sequence": result.first_invalid_sequence,
                "entries_checked": result.entries_checked,
            },
            current_time,
        )
        return result

    def wait_time_report(self) -> dict[str, float | int | None]:
        waits = sorted(
            (request.arrival_at - request.accepted_at).total_seconds()
            for request in self.hall_requests.values()
            if request.arrival_at is not None
        )
        if not waits:
            return {"count": 0, "average_seconds": None, "median_seconds": None, "p95_seconds": None}
        rank = max(0, min(len(waits) - 1, int((len(waits) * 0.95 + 0.999999)) - 1))
        return {
            "count": len(waits),
            "average_seconds": sum(waits) / len(waits),
            "median_seconds": median(waits),
            "p95_seconds": waits[rank],
        }

    def fleet_status(self) -> dict[str, ElevatorState]:
        return dict(self.states)
