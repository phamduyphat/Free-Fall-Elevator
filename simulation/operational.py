"""Deterministic Mode-1 elevator *test stub* and direction-aware dispatch.

This is a software-only demonstration behind the logical elevator interface. It
is not physical equipment telemetry, an emergency controller or a safety system.
The state is process-local and must not be used as a production data store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, median
import threading
import time
from typing import Literal
from uuid import uuid4

CarAvailability = Literal["IN_SERVICE", "OCCUPIED", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"]
HallDirection = Literal["UP", "DOWN"]
CallType = Literal["ORDINARY", "FREIGHT"]
TERMINAL_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}


class OperationalError(ValueError):
    """A request violates operational dispatch or floor constraints."""


def _position(floor: int) -> int:
    """B1=-1 is adjacent to 1 (there is no Floor 0)."""
    return 0 if floor == -1 else floor


def _floor_from_position(position: int) -> int:
    return -1 if position == 0 else position


@dataclass
class Stop:
    floor: int
    kind: Literal["HALL", "CAR", "SERVICE", "MEDICAL", "RESCUE"]
    request_id: str


@dataclass
class Car:
    elevator_id: str
    current_floor: int = 1
    direction: str = "IDLE"
    door_state: str = "CLOSED"
    availability: CarAvailability = "IN_SERVICE"
    connected: bool = True
    last_seen_tick: int = 0
    stops: list[Stop] = field(default_factory=list)
    dwell_remaining: int = 0
    medical_run: bool = False
    rescue_active: bool = False
    rescue_wait_remaining: int = 0
    active_fault: str | None = None
    obstruction_started: int | None = None
    obstruction_alerted: bool = False
    message: str = ""


@dataclass
class HallRequest:
    request_id: str
    origin_floor: int
    call_type: CallType
    direction: HallDirection | None
    accepted_at: datetime
    status: str = "RECEIVED"
    assigned_car: str | None = None
    arrived_at: datetime | None = None
    wait_seconds: float | None = None
    reassignment_count: int = 0
    boarding_evidence: bool | None = None
    boarding_deadline_tick: int | None = None
    no_boarding: bool = False
    traffic_profile: str | None = None
    history: list[str] = field(default_factory=lambda: ["RECEIVED"])

    def transition(self, new_status: str) -> None:
        if new_status != self.status:
            self.status = new_status
            self.history.append(new_status)


@dataclass
class CarRequest:
    request_id: str
    elevator_id: str
    destination_floor: int
    accepted_at: datetime
    status: str = "QUEUED"
    completed_at: datetime | None = None
    origin_floor: int | None = None
    is_service_run: bool = False


@dataclass
class Incident:
    incident_id: str
    kind: str
    elevator_id: str | None
    status: str
    severity: str
    created_at: datetime
    details: str
    floor: int | None = None
    acknowledged_by: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    rescue_car_id: str | None = None
    confirmed: bool = False
    escalated: bool = False
    history: list[str] = field(default_factory=lambda: ["OPEN"])


class OperationalSimulator:
    """Thread-safe, single-process in-memory simulator; each simulated second is 1 floor step.

    Request state is kept in memory. Normal calls use E1-E4, freight calls E5.
    Only an ACS-authorized caller should invoke submit_car_call/start_service_run.
    """

    ELEVATOR_IDS = ("E1", "E2", "E3", "E4", "E5")

    def __init__(self, *, auto_advance: bool = False) -> None:
        self._lock = threading.RLock()
        self._auto_advance = auto_advance
        self.reset()

    def reset(self, *, base_time: datetime | None = None) -> None:
        with self._lock:
            self.cars: dict[str, Car] = {eid: Car(eid) for eid in self.ELEVATOR_IDS}
            self.hall_calls: dict[str, HallRequest] = {}
            self.car_calls: dict[str, CarRequest] = {}
            self.incidents: dict[str, Incident] = {}
            self.events: list[dict] = []
            self.trips: list[dict] = []
            self._active_leg: dict[str, tuple[int, datetime]] = {}
            self.downtime_intervals: list[dict] = []
            self.heartbeats: list[dict] = []
            self._emergency_triggers: dict[str, list[int]] = {}
            self.sim_tick = 0
            self.base_time = base_time if base_time is not None else datetime.now(timezone.utc)
            self._wall_reference = time.monotonic()
            self.heartbeats.append({"timestamp": self._now(), "status": "UP"})

    def _now(self) -> datetime:
        return self.base_time + timedelta(seconds=self.sim_tick)

    @staticmethod
    def _valid_floor(elevator_id: str, floor: int) -> bool:
        return type(floor) is int and (1 <= floor <= 30 or (floor == -1 and elevator_id != "E5"))

    def _eligible(self, car: Car) -> bool:
        return (car.connected and car.availability == "IN_SERVICE" and not car.rescue_active
                and self.sim_tick - car.last_seen_tick <= 5)

    def _update_auto(self) -> None:
        if not self._auto_advance:
            return
        now = time.monotonic()
        steps = min(600, max(0, int(now - self._wall_reference)))
        if steps:
            self._advance_locked(steps)
            self._wall_reference += steps
            # Do not run an unbounded replay after long periods without clients.
            if now - self._wall_reference > 600:
                self._wall_reference = now

    def _eta_and_insertion(self, car: Car, floor: int, direction: HallDirection | None) -> tuple[int, int]:
        """Predicted pickup time and insertion position in committed stop order.

        A matching-direction request on the current route may be picked up on
        the way. All other requests are appended after existing commitments.
        Equal ETA ties are resolved by lower car ID in _assign_request.
        """
        position = _position(car.current_floor)
        target = _position(floor)
        if not car.stops:
            return abs(position - target) + car.dwell_remaining, 0

        first = _position(car.stops[0].floor)
        travel_direction = "UP" if first > position else "DOWN" if first < position else "IDLE"
        along_first_leg = (min(position, first) <= target <= max(position, first))
        if along_first_leg and direction == travel_direction and car.dwell_remaining == 0:
            return abs(position - target), 0

        estimate = car.dwell_remaining
        previous = position
        for stop in car.stops:
            next_pos = _position(stop.floor)
            estimate += abs(next_pos - previous) + 2  # 2s door dwell per scheduled stop
            previous = next_pos
        estimate += abs(previous - target)
        return estimate, len(car.stops)

    def _assign_request(self, request: HallRequest) -> None:
        if request.status in TERMINAL_STATUSES or request.assigned_car is not None:
            return
        if request.call_type == "FREIGHT":
            eligible = [self.cars["E5"]] if self._eligible(self.cars["E5"]) else []
        else:
            eligible = [self.cars[eid] for eid in self.ELEVATOR_IDS[:4] if self._eligible(self.cars[eid])]
        if not eligible:
            request.transition("QUEUED")
            return
        choices = [(self._eta_and_insertion(car, request.origin_floor, request.direction),
                    int(car.elevator_id[1:]), car) for car in eligible]
        (eta, index), _, chosen = min(choices, key=lambda entry: (entry[0][0], entry[1]))
        chosen.stops.insert(index, Stop(request.origin_floor, "HALL", request.request_id))
        self._event("ASSIGNMENT", chosen.elevator_id, f"Hall call {request.request_id} assigned")
        request.assigned_car = chosen.elevator_id
        request.transition("ASSIGNED")

    def _reassign_pending(self) -> None:
        # Reassignment occurs after a car has been isolated and also when an
        # eligible car returns while calls were queued.
        for request in self.hall_calls.values():
            if request.status == "QUEUED" and request.assigned_car is None:
                self._assign_request(request)

    def submit_hall_call(self, *, origin_floor: int, direction: HallDirection | None = None,
                         call_type: CallType = "ORDINARY",
                         traffic_profile: str | None = None) -> tuple[dict, bool]:
        with self._lock:
            self._update_auto()
            if call_type not in ("ORDINARY", "FREIGHT"):
                raise OperationalError("call_type must be ORDINARY or FREIGHT")
            if type(origin_floor) is not int or not (origin_floor == -1 or 1 <= origin_floor <= 30):
                raise OperationalError("Hall floor must be B1 (-1) or 1..30")
            if call_type == "FREIGHT":
                if origin_floor == -1:
                    raise OperationalError("E5 does not serve B1")
                if direction is not None:
                    raise OperationalError("Freight calls do not specify UP/DOWN")
                if not self._eligible(self.cars["E5"]):
                    raise OperationalError("E5 is unavailable; freight call rejected, not queued")
            else:
                if direction not in ("UP", "DOWN"):
                    raise OperationalError("Ordinary hall calls require UP or DOWN")
                if (origin_floor == -1 and direction != "UP") or (origin_floor == 30 and direction != "DOWN"):
                    raise OperationalError("Invalid travel direction for endpoint floor")

            for previous in self.hall_calls.values():
                if (previous.status not in TERMINAL_STATUSES
                    and previous.origin_floor == origin_floor
                    and previous.direction == direction
                    and previous.call_type == call_type):
                    return self._serialize_request(previous), True

            request = HallRequest(
                str(uuid4()),
                origin_floor,
                call_type,
                direction,
                self._now(),
                traffic_profile=traffic_profile,
            )
            self.hall_calls[request.request_id] = request
            self._event("HALL_CALL", None, f"{call_type} hall call at {origin_floor}")
            request.transition("QUEUED")
            self._assign_request(request)
            return self._serialize_request(request), False

    def submit_car_call(self, elevator_id: str, destination_floor: int) -> dict:
        """Call ONLY after ACS has granted this destination (SEC-2.6)."""
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if not self._valid_floor(elevator_id, destination_floor):
                raise OperationalError("Invalid destination or E5-to-B1 is forbidden")
            if not self._eligible(car):
                raise OperationalError("Elevator is unavailable for a destination request")
            request = CarRequest(
                str(uuid4()),
                elevator_id,
                destination_floor,
                self._now(),
                origin_floor=car.current_floor,
            )
            self.car_calls[request.request_id] = request
            self._event("CAR_CALL", elevator_id, f"Authorized destination {destination_floor}")
            car.stops.append(Stop(destination_floor, "CAR", request.request_id))
            return asdict(request)

    def start_service_run(self, destination_floor: int) -> dict:
        """Call ONLY after ACS has granted a service trip on E5."""
        with self._lock:
            self._update_auto()
            car = self.cars["E5"]
            if not self._eligible(car) or car.stops:
                raise OperationalError("E5 has active commitments or is unavailable")
            if not self._valid_floor("E5", destination_floor):
                raise OperationalError("Invalid E5 service-run destination")
            car.availability = "OCCUPIED"
            request = CarRequest(
                str(uuid4()),
                "E5",
                destination_floor,
                self._now(),
                origin_floor=car.current_floor,
                is_service_run=True,
            )
            self.car_calls[request.request_id] = request
            car.stops.append(Stop(destination_floor, "SERVICE", request.request_id))
            return asdict(request)

    def _car(self, elevator_id: str) -> Car:
        if elevator_id not in self.cars:
            raise OperationalError(f"Unknown elevator: {elevator_id}")
        return self.cars[elevator_id]

    def _release_car_hall_calls(self, car: Car) -> None:
        for stop in car.stops:
            if stop.kind == "HALL":
                request = self.hall_calls[stop.request_id]
                if request.status not in TERMINAL_STATUSES:
                    request.assigned_car = None
                    request.reassignment_count += 1
                    request.transition("QUEUED")
            elif stop.kind in ("CAR", "SERVICE"):
                request = self.car_calls[stop.request_id]
                request.status = "BLOCKED/REQUIRES_RECONCILIATION"
        car.stops.clear()
        self._active_leg.pop(car.elevator_id, None)
        car.medical_run = False
        car.rescue_active = False
        car.rescue_wait_remaining = 0
        car.direction = "IDLE"

    def _close_open_downtime(self, elevator_id: str, now: datetime) -> None:
        for interval in self.downtime_intervals:
            if interval["elevator_id"] == elevator_id and interval["ended_at"] is None:
                interval["ended_at"] = now
                interval["duration_seconds"] = max(
                    0.0, (now - interval["started_at"]).total_seconds()
                )

    def isolate(self, elevator_id: str, availability: CarAvailability) -> None:
        """Trusted equipment-stub or authorized staff transition, not public passenger input."""
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if availability not in ("IN_SERVICE", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"):
                raise OperationalError("Unsupported operational state")
            if car.availability == "EMERGENCY_UNAVAILABLE" and availability in ("MAINTENANCE", "FAULT"):
                raise OperationalError("Emergency state cannot be downgraded to maintenance or fault")
            if availability == "IN_SERVICE":
                recovered_snapshot = (car.availability == "STALE" and car.connected
                                      and self.sim_tick - car.last_seen_tick <= 5
                                      and car.active_fault is None and car.door_state != "OBSTRUCTED")
                if not (self.recoverable(elevator_id) or recovered_snapshot):
                    raise OperationalError("Recovery checks failed: stale, fault, door or movement state")
                if any(i.kind in ("FIRE", "POWER_FAILURE", "MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                       and i.status not in ("RESOLVED", "CLOSED")
                       and (i.elevator_id is None or i.elevator_id == elevator_id)
                       for i in self.incidents.values()):
                    raise OperationalError("Unresolved incident prevents return to service")
            now = self._now()
            if availability != "IN_SERVICE":
                self._release_car_hall_calls(car)
                self._event("ISOLATION", elevator_id, availability)
                if car.availability != availability:
                    self._close_open_downtime(elevator_id, now)
                    mapped_state = (
                        "EMERGENCY"
                        if availability == "EMERGENCY_UNAVAILABLE"
                        else "FAULT"
                        if availability in ("FAULT", "STALE")
                        else "MAINTENANCE"
                    )
                    reason = (
                        "COMMUNICATION_LOSS"
                        if availability == "STALE"
                        else (car.active_fault or availability)
                    )
                    self.downtime_intervals.append(
                        {
                            "interval_id": str(uuid4()),
                            "elevator_id": elevator_id,
                            "state": mapped_state,
                            "reason": reason,
                            "started_at": now,
                            "ended_at": None,
                            "duration_seconds": None,
                            "technician_id": None,
                        }
                    )
            else:
                self._close_open_downtime(elevator_id, now)
            car.availability = availability
            if availability == "IN_SERVICE":
                car.connected = True
                car.last_seen_tick = self.sim_tick
                car.door_state = "CLOSED"
            if availability == "STALE":
                car.connected = False
            self._reassign_pending()

    def recoverable(self, elevator_id: str) -> bool:
        """Stub-side recovery prerequisites: fresh, connected and unobstructed.

        This is not proof that an underlying physical fault was cleared.
        """
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            return (car.connected and self.sim_tick - car.last_seen_tick <= 5
                    and car.door_state != "OBSTRUCTED" and car.active_fault is None
                    and car.availability != "STALE" and not car.medical_run and not car.rescue_active
                    and not any(i.kind in ("FIRE", "POWER_FAILURE")
                                and i.status not in ("RESOLVED", "CLOSED")
                                for i in self.incidents.values()))

    def can_return_to_service(self, elevator_id: str) -> bool:
        with self._lock:
            self._update_auto()
            return self.recoverable(elevator_id) and not any(
                i.kind in ("FIRE", "POWER_FAILURE", "MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                and i.status not in ("RESOLVED", "CLOSED")
                and (i.elevator_id is None or i.elevator_id == elevator_id)
                for i in self.incidents.values())

    def _serve_stop(self, car: Car) -> None:
        stop = car.stops.pop(0)
        now = self._now()
        orig_floor, dep_time = self._active_leg.pop(car.elevator_id, (car.current_floor, now))
        traversed = abs(_position(stop.floor) - _position(orig_floor))
        travel_sec = max(0.0, (now - dep_time).total_seconds())
        if traversed > 0 or stop.kind in ("CAR", "SERVICE"):
            self.trips.append(
                {
                    "trip_id": str(uuid4()),
                    "elevator_id": car.elevator_id,
                    "origin_floor": orig_floor,
                    "destination_floor": stop.floor,
                    "departure_time": dep_time,
                    "arrival_time": now,
                    "travel_time_seconds": travel_sec,
                    "floors_traversed": traversed,
                    "motor_starts": 1,
                    "is_estimated": False,
                    "is_service_run": stop.kind == "SERVICE",
                    "traffic_profile": None,
                }
            )
        car.door_state = "OPEN"
        car.dwell_remaining = 2
        car.direction = "IDLE"
        if stop.kind == "HALL":
            request = self.hall_calls[stop.request_id]
            request.transition("ARRIVED")
            request.arrived_at = now
            request.wait_seconds = (request.arrived_at - request.accepted_at).total_seconds()
            request.boarding_deadline_tick = self.sim_tick + 30
            request.transition("COMPLETED")
            self._event("HALL_ARRIVAL", car.elevator_id, f"Wait {request.wait_seconds:.0f}s")
        elif stop.kind == "MEDICAL":
            car.medical_run = False
            car.message = "Responder floor reached; staff assistance requested"
            self._event("MEDICAL_ARRIVAL", car.elevator_id, "Responder floor reached")
        elif stop.kind == "RESCUE":
            incident = self.incidents[stop.request_id]
            car.dwell_remaining = car.rescue_wait_remaining
            car.message = "Rescue car waiting for Admin boarding confirmation"
            self._event("RESCUE_ARRIVAL", car.elevator_id, incident.incident_id)
        else:
            request = self.car_calls[stop.request_id]
            request.status = "COMPLETED"
            request.completed_at = now
            if stop.kind == "SERVICE":
                car.availability = "IN_SERVICE"

    def _advance_locked(self, seconds: int) -> None:
        for _ in range(seconds):
            self.sim_tick += 1
            if self.sim_tick % 10 == 0:
                self.heartbeats.append({"timestamp": self._now(), "status": "UP"})
            self._escalate_incidents()
            self._detect_empty_calls()
            for car in self.cars.values():
                if car.connected:
                    car.last_seen_tick = self.sim_tick  # test stub heartbeat, not physical telemetry
                if car.obstruction_started is not None and not car.obstruction_alerted \
                        and self.sim_tick - car.obstruction_started >= 10:
                    car.obstruction_alerted = True
                    self._create_incident("DOOR_OBSTRUCTION", car.elevator_id,
                                          "Door obstruction persisted at least 10 simulated seconds", "WARNING")
                if car.availability not in ("IN_SERVICE", "OCCUPIED") and not car.medical_run:
                    continue
                if car.dwell_remaining:
                    car.dwell_remaining -= 1
                    if not car.dwell_remaining:
                        if car.rescue_active and not car.stops:
                            car.rescue_active = False
                            car.rescue_wait_remaining = 0
                            car.message = "Rescue wait expired; incident remains open"
                            self._event("RESCUE_RELEASE", car.elevator_id, "Wait expired")
                        car.door_state = "CLOSED"
                    continue
                if not car.stops:
                    car.direction = "IDLE"
                    continue
                if car.obstruction_started is not None:
                    car.direction = "IDLE"  # synthetic interlock; never move with an active obstruction
                    continue
                if car.elevator_id not in self._active_leg:
                    self._active_leg[car.elevator_id] = (
                        car.current_floor,
                        self._now() - timedelta(seconds=1),
                    )
                target = _position(car.stops[0].floor)
                current = _position(car.current_floor)
                car.direction = "UP" if target > current else "DOWN" if target < current else "IDLE"
                if target != current:
                    current += 1 if target > current else -1
                    car.current_floor = _floor_from_position(current)
                if car.stops and car.stops[0].kind == "HALL":
                    request = self.hall_calls[car.stops[0].request_id]
                    if request.status == "ASSIGNED":
                        request.transition("APPROACHING")
                if car.stops and car.stops[0].kind in ("CAR", "SERVICE"):
                    request = self.car_calls[car.stops[0].request_id]
                    if request.status == "QUEUED":
                        request.status = "IN_TRANSIT"
                if current == target:
                    self._serve_stop(car)
            self._reassign_pending()

    def step_from_clock(self, seconds: int) -> None:
        """Advance simulator from `SimulationClock` tick (fast-forwards when completely idle)."""
        if seconds <= 0:
            return
        with self._lock:
            is_idle = (
                all(
                    not c.stops
                    and c.dwell_remaining == 0
                    and c.obstruction_started is None
                    and not c.rescue_active
                    for c in self.cars.values()
                )
                and not any(
                    i.status not in ("RESOLVED", "CLOSED") and not i.escalated
                    for i in self.incidents.values()
                )
                and not any(
                    r.status == "COMPLETED"
                    and r.boarding_evidence is False
                    and not r.no_boarding
                    and r.boarding_deadline_tick is not None
                    and self.sim_tick < r.boarding_deadline_tick
                    for r in self.hall_calls.values()
                )
            )
            if is_idle and seconds > 60:
                self.sim_tick += seconds
                for car in self.cars.values():
                    if car.connected:
                        car.last_seen_tick = self.sim_tick
                self.heartbeats.append({"timestamp": self._now(), "status": "UP"})
            else:
                self._advance_locked(seconds)
            self._wall_reference = time.monotonic()

    def advance(self, seconds: int = 1) -> dict:
        """Deterministic stepping for tests, without a hardware control command."""
        if type(seconds) is not int or not (0 <= seconds <= 10000):
            raise OperationalError("seconds must be an integer between 0 and 10000")
        with self._lock:
            self._advance_locked(seconds)
            self._wall_reference = time.monotonic()
            return self.fleet_status()

    @staticmethod
    def _serialize_request(request: HallRequest) -> dict:
        return asdict(request)

    def hall_call_status(self, request_id: str | None = None) -> list[dict] | dict:
        with self._lock:
            self._update_auto()
            if request_id:
                if request_id not in self.hall_calls:
                    raise OperationalError("Unknown hall-call ID")
                return self._serialize_request(self.hall_calls[request_id])
            return [self._serialize_request(req) for req in self.hall_calls.values()]

    def fleet_status(self) -> dict:
        with self._lock:
            self._update_auto()
            return {
                "source": "labelled-test-stub",
                "sim_tick": self.sim_tick,
                "elevators": [{
                    "elevator_id": car.elevator_id,
                    "state": car.availability,
                    "current_floor": car.current_floor,
                    "direction": car.direction,
                    "door_state": car.door_state,
                    "available": self._eligible(car),
                    "target_floor": car.stops[0].floor if car.stops else None,
                    "pending_stops": len(car.stops),
                    "active_fault": car.active_fault,
                    "medical_run": car.medical_run,
                    "rescue_active": car.rescue_active,
                    "message_to_occupants": car.message,
                    "message": "Software test-stub state; not connected to physical elevator equipment.",
                } for car in self.cars.values()],
            }


    def _event(self, event_type: str, elevator_id: str | None, detail: str) -> None:
        self.events.append({"seq": len(self.events) + 1, "sim_tick": self.sim_tick,
                            "timestamp": self._now().isoformat(), "event_type": event_type,
                            "elevator_id": elevator_id, "detail": detail})

    def _create_incident(self, kind: str, elevator_id: str | None,
                         details: str, severity: str = "HIGH", floor: int | None = None) -> Incident:
        incident = Incident(str(uuid4()), kind, elevator_id, "OPEN", severity,
                            self._now(), details, floor=floor)
        self.incidents[incident.incident_id] = incident
        self._event("INCIDENT_OPEN", elevator_id, f"{kind}: {incident.incident_id}")
        return incident

    def _detect_empty_calls(self) -> None:
        for request in self.hall_calls.values():
            if (request.status == "COMPLETED" and request.boarding_evidence is False
                    and not request.no_boarding and request.boarding_deadline_tick is not None
                    and self.sim_tick >= request.boarding_deadline_tick):
                request.no_boarding = True
                self._event("NO_BOARDING", request.assigned_car,
                            f"No boarding evidence for hall call {request.request_id}")
                recent = [r for r in self.hall_calls.values()
                          if r.no_boarding and r.origin_floor == request.origin_floor
                          and r.boarding_deadline_tick is not None
                          and self.sim_tick - (r.boarding_deadline_tick + 0) <= 600]
                if len(recent) >= 5 and not any(i.kind == "REPEATED_EMPTY_CALLS"
                                                     and i.floor == request.origin_floor
                                                     and i.status not in ("RESOLVED", "CLOSED")
                                                     for i in self.incidents.values()):
                    self._create_incident("REPEATED_EMPTY_CALLS", None,
                        "At least five confirmed empty hall calls in ten simulated minutes",
                        "LOW", request.origin_floor)

    def boarding_evidence(self, request_id: str, boarded: bool) -> dict:
        """Only an explicitly labelled equipment *test stub* may supply boarding evidence."""
        with self._lock:
            self._update_auto()
            request = self.hall_calls.get(request_id)
            if request is None or request.status != "COMPLETED":
                raise OperationalError("A completed hall call is required")
            if request.boarding_deadline_tick is not None and self.sim_tick > request.boarding_deadline_tick:
                raise OperationalError("Boarding observation window has expired")
            request.boarding_evidence = boarded
            self._event("BOARDING_EVIDENCE", request.assigned_car,
                        f"Test stub boarded={boarded}; hall call {request_id}")
            return self._serialize_request(request)

    def cancel_hall_call(self, request_id: str, actor: str) -> dict:
        with self._lock:
            self._update_auto()
            request = self.hall_calls.get(request_id)
            if request is None or request.status in TERMINAL_STATUSES:
                raise OperationalError("Unknown or terminal hall call")
            if request.assigned_car:
                car = self.cars[request.assigned_car]
                car.stops = [stop for stop in car.stops if stop.request_id != request_id]
            request.assigned_car = None
            request.transition("CANCELLED")
            self._event("HALL_CANCELLED", None, f"{request_id} by {actor}")
            return self._serialize_request(request)

    def diagnostics(self, elevator_id: str) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            return {"source": "labelled-test-stub", "car": asdict(car),
                    "state_fresh": car.connected and self.sim_tick - car.last_seen_tick <= 5,
                    "recovery_ready": self.can_return_to_service(elevator_id),
                    "incidents": [asdict(i) for i in self.incidents.values()
                                  if i.elevator_id == elevator_id],
                    "recent_events": [e for e in self.events if e["elevator_id"] == elevator_id][-20:]}

    def _escalate_incidents(self) -> None:
        for incident in self.incidents.values():
            if (incident.status not in ("RESOLVED", "CLOSED") and not incident.escalated
                    and (self._now() - incident.created_at).total_seconds() >= 60):
                incident.escalated = True
                incident.history.append("ESCALATED")
                self._event("INCIDENT_ESCALATION", incident.elevator_id,
                            f"Repeat security notification: {incident.incident_id}")

    def trigger_emergency(self, elevator_id: str, kind: str) -> dict:
        """Unauthenticated in-car HELP or MEDICAL event, test stub only (EMR-3.1)."""
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if kind not in ("HELP", "MEDICAL"):
                raise OperationalError("Emergency option must be HELP or MEDICAL")
            incident = self._create_incident(kind, elevator_id,
                "Passenger requested assistance from inside the simulated car",
                "CRITICAL" if kind == "MEDICAL" else "HIGH", car.current_floor)
            triggers = self._emergency_triggers.setdefault(elevator_id, [])
            triggers[:] = [tick for tick in triggers if self.sim_tick - tick <= 60]
            triggers.append(self.sim_tick)
            if len(triggers) == 4:
                self._create_incident("REPEATED_EMERGENCY", elevator_id,
                                      "More than three triggers within 60 simulated seconds", "LOW")
            car.message = "Help requested; security has been notified (stub)"
            if kind == "MEDICAL":
                self.isolate(elevator_id, "EMERGENCY_UNAVAILABLE")
                car.message = "Medical assistance requested (stub)"
                active_building = any(i.kind in ("FIRE", "POWER_FAILURE")
                                      and i.status not in ("RESOLVED", "CLOSED")
                                      for i in self.incidents.values())
                if car.connected and not active_building and car.active_fault is None:
                    car.medical_run = True
                    car.stops.append(Stop(1, "MEDICAL", incident.incident_id))
                    self._event("MEDICAL_RESPONDER_RUN", elevator_id,
                                "Nonstop run to Floor 1 scheduled in the stub")
            return asdict(incident)

    def confirm_help_as_stuck(self, incident_id: str) -> dict:
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if incident.kind != "HELP" or incident.status in ("RESOLVED", "CLOSED"):
                raise OperationalError("Only an open HELP request can be confirmed as stuck")
            incident.kind = "STUCK_CAR"
            incident.confirmed = True
            incident.history.append("CONFIRMED_STUCK")
            self.isolate(incident.elevator_id, "EMERGENCY_UNAVAILABLE")
            return asdict(incident)

    def building_emergency(self, kind: str) -> dict:
        """Inject building fire or power status; default policy commands NO recall."""
        with self._lock:
            self._update_auto()
            if kind not in ("FIRE", "POWER_FAILURE"):
                raise OperationalError("Building event must be FIRE or POWER_FAILURE")
            incident = self._create_incident(kind, None,
                "Simulated building-wide event. Ordinary dispatch suspended; no recall.", "CRITICAL")
            for elevator_id in self.ELEVATOR_IDS:
                self.isolate(elevator_id, "EMERGENCY_UNAVAILABLE")
                self.cars[elevator_id].message = f"{kind} reported: wait for staff instructions"
            return asdict(incident)

    def report_fault(self, elevator_id: str, fault_code: str) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if not fault_code.strip() or len(fault_code) > 80:
                raise OperationalError("Fault code must contain 1..80 characters")
            car.active_fault = fault_code.strip()
            if car.availability != "EMERGENCY_UNAVAILABLE":
                self.isolate(elevator_id, "FAULT")
            return asdict(self._create_incident("BLOCKING_FAULT", elevator_id, fault_code.strip()))

    def clear_fault(self, elevator_id: str) -> dict:
        """Simulated fault-clear sensor event; does not itself restore service."""
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if car.active_fault is None:
                raise OperationalError("No recorded active fault")
            self._event("FAULT_CLEARED", elevator_id, car.active_fault)
            car.active_fault = None
            return self.fleet_status()

    def door_obstruction(self, elevator_id: str, active: bool) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            car.obstruction_started = self.sim_tick if active else None
            car.obstruction_alerted = False
            car.door_state = "OBSTRUCTED" if active else "OPEN"
            self._event("OBSTRUCTION" if active else "OBSTRUCTION_CLEARED", elevator_id,
                        "synthetic sensor event")
            return self.fleet_status()

    def passenger_door_command(self, elevator_id: str, action: str) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if action not in ("OPEN", "CLOSE"):
                raise OperationalError("Door action must be OPEN or CLOSE")
            if not car.connected or car.availability not in ("IN_SERVICE", "OCCUPIED"):
                raise OperationalError("Door operation prohibited in current car state")
            if car.direction != "IDLE" and car.stops:
                raise OperationalError("Door operation prohibited while car is moving")
            if action == "CLOSE" and (car.door_state == "OBSTRUCTED" or car.obstruction_started is not None):
                raise OperationalError("Door close prohibited while obstruction is active")
            car.door_state = "OPEN" if action == "OPEN" else "CLOSED"
            car.dwell_remaining = 2 if action == "OPEN" else 0
            self._event("DOOR_COMMAND", elevator_id, action)
            return self.fleet_status()

    def communication_loss(self, elevator_id: str) -> dict:
        with self._lock:
            self._update_auto()
            self.isolate(elevator_id, "STALE")
            return asdict(self._create_incident("COMMUNICATION_LOSS", elevator_id,
                "Connection lost; outstanding commands require reconciliation"))

    def reconcile_connection(self, elevator_id: str) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if car.availability != "STALE":
                raise OperationalError("Car is not marked STALE")
            car.connected = True
            car.last_seen_tick = self.sim_tick  # simulated fresh snapshot
            if car.active_fault or car.door_state == "OBSTRUCTED":
                raise OperationalError("Cannot reconcile with active fault or obstruction")
            for request in self.car_calls.values():
                if request.elevator_id == elevator_id and request.status == "BLOCKED/REQUIRES_RECONCILIATION":
                    request.status = "FAILED"  # unknown outcome: never blindly replay
            self.isolate(elevator_id, "IN_SERVICE")
            self._event("RECONCILED", elevator_id, "Fresh simulated snapshot; no uncertain command replay")
            return self.fleet_status()

    def _incident(self, incident_id: str) -> Incident:
        if incident_id not in self.incidents:
            raise OperationalError("Unknown incident ID")
        return self.incidents[incident_id]

    def phone_medical_incident(self, floor: int) -> dict:
        with self._lock:
            self._update_auto()
            if type(floor) is not int or not 1 <= floor <= 30:
                raise OperationalError("Medical rescue floor must be 1..30")
            return asdict(self._create_incident("MEDICAL", None,
                             "Admin-reported medical emergency by telephone", "CRITICAL", floor))

    def dispatch_rescue(self, incident_id: str, *, wait_seconds: int = 120,
                        confirmed_empty: bool = False) -> dict:
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if incident.kind not in ("MEDICAL", "STUCK_CAR") or incident.status in ("RESOLVED", "CLOSED"):
                raise OperationalError("Rescue requires an unresolved medical or stuck-car incident")
            if not confirmed_empty:
                raise OperationalError("Admin must confirm rescue car is empty")
            if type(wait_seconds) is not int or not 1 <= wait_seconds <= 600:
                raise OperationalError("Rescue wait must be 1..600 seconds")
            if incident.rescue_car_id is not None:
                raise OperationalError("Incident already has a rescue car")
            floor = incident.floor
            if floor is None:
                raise OperationalError("Incident floor is unknown")
            candidates = [c for c in list(self.cars.values())[:4]
                          if c.elevator_id != incident.elevator_id and self._eligible(c)
                          and all(s.kind == "HALL" for s in c.stops)]
            if not candidates:
                raise OperationalError("No eligible, confirmed-empty rescue car")
            car = min(candidates, key=lambda c: (self._eta_and_insertion(c, floor, None)[0],
                                                 int(c.elevator_id[1:])))
            self._release_car_hall_calls(car)
            car.rescue_active = True
            car.rescue_wait_remaining = wait_seconds
            car.stops.append(Stop(floor, "RESCUE", incident.incident_id))
            incident.rescue_car_id = car.elevator_id
            self._event("RESCUE_DISPATCH", car.elevator_id, f"Incident {incident_id}, wait {wait_seconds}s")
            self._reassign_pending()
            return {"incident": asdict(incident), "rescue_car_id": car.elevator_id,
                    "wait_seconds": wait_seconds}

    def confirm_rescue_boarding(self, incident_id: str) -> dict:
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if incident.kind != "MEDICAL" or not incident.rescue_car_id:
                raise OperationalError("Only a medical rescue can confirm boarding")
            car = self.cars[incident.rescue_car_id]
            if not car.rescue_active or car.stops or car.door_state != "OPEN":
                raise OperationalError("Rescue car must be waiting with doors open")
            car.rescue_active = False
            car.rescue_wait_remaining = 0
            car.dwell_remaining = 0
            car.door_state = "CLOSED"
            incident.elevator_id = car.elevator_id
            self.isolate(car.elevator_id, "EMERGENCY_UNAVAILABLE")
            car.medical_run = True
            car.stops.append(Stop(1, "MEDICAL", incident_id))
            self._event("RESCUE_BOARDING", car.elevator_id, "Medical responder-floor run")
            return asdict(incident)

    def incident_action(self, incident_id: str, action: str, *, actor: str,
                        technician_confirmation: bool = False) -> dict:
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if action == "ACKNOWLEDGE":
                if incident.status != "OPEN":
                    raise OperationalError("Only OPEN incidents can be acknowledged")
                incident.status = "ACKNOWLEDGED"
                incident.acknowledged_by = actor
            elif action == "RESOLVE":
                if incident.status not in ("OPEN", "ACKNOWLEDGED"):
                    raise OperationalError("Incident already resolved or closed")
                if incident.kind != "HELP" and not technician_confirmation:
                    raise OperationalError("Recovery confirmation is required")
                if incident.elevator_id:
                    car = self.cars[incident.elevator_id]
                    if car.rescue_active:
                        raise OperationalError("Rescue car still active")
                    if car.availability in ("EMERGENCY_UNAVAILABLE", "FAULT", "MAINTENANCE", "STALE"):
                        if not self.recoverable(incident.elevator_id):
                            raise OperationalError("Recovery checks not satisfied")
                incident.status = "RESOLVED"
                incident.resolved_at = self._now()
                incident.resolved_by = actor
                if incident.kind in ("FIRE", "POWER_FAILURE"):
                    if not any(i is not incident and i.kind in ("FIRE", "POWER_FAILURE")
                               and i.status not in ("RESOLVED", "CLOSED") for i in self.incidents.values()):
                        for car in self.cars.values():
                            if car.availability == "EMERGENCY_UNAVAILABLE":
                                if self.recoverable(car.elevator_id):
                                    self.isolate(car.elevator_id, "IN_SERVICE")
                elif incident.elevator_id:
                    car = self.cars[incident.elevator_id]
                    if car.availability in ("EMERGENCY_UNAVAILABLE", "FAULT", "MAINTENANCE"):
                        if not any(i is not incident and i.elevator_id == incident.elevator_id
                                   and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                                   and i.status not in ("RESOLVED", "CLOSED")
                                   for i in self.incidents.values()):
                            self.isolate(incident.elevator_id, "IN_SERVICE")
            elif action == "CLOSE":
                if incident.status != "RESOLVED":
                    raise OperationalError("Resolve before closing")
                incident.status = "CLOSED"
            else:
                raise OperationalError("Action must be ACKNOWLEDGE, RESOLVE or CLOSE")
            incident.history.append(incident.status)
            self._event("INCIDENT_" + action, incident.elevator_id, f"{incident_id} by {actor}")
            return asdict(incident)

    def list_incidents(self) -> list[dict]:
        with self._lock:
            self._update_auto()
            return [asdict(incident) for incident in self.incidents.values()]

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            self._update_auto()
            return list(self.events[-limit:])

    def metrics(self) -> dict:
        """Aggregate **in-memory** stub-only measurements, not persisted ANA reports."""
        with self._lock:
            self._update_auto()
            waits = sorted(r.wait_seconds for r in self.hall_calls.values()
                           if r.wait_seconds is not None)
            def pct(p: float) -> float | None:
                if not waits:
                    return None
                from math import ceil
                return waits[max(0, ceil(p * len(waits)) - 1)]
            completed_trips = [r for r in self.car_calls.values() if r.status == "COMPLETED"]
            active_incidents = [i for i in self.incidents.values() if i.status not in ("RESOLVED", "CLOSED")]
            return {"source": "labelled-test-stub", "sim_tick": self.sim_tick,
                    "total_hall_calls": len(self.hall_calls), "completed_hall_calls": len(waits),
                    "active_hall_calls": sum(r.status not in TERMINAL_STATUSES for r in self.hall_calls.values()),
                    "mean_wait_seconds": mean(waits) if waits else None,
                    "median_wait_seconds": median(waits) if waits else None,
                    "p95_wait_seconds": pct(.95),
                    "completed_car_calls": len(completed_trips),
                    "active_incidents": len(active_incidents),
                    "fleet_available": sum(self._eligible(c) for c in self.cars.values()),
                    "fleet_count": len(self.cars), "events_count": len(self.events)}


# The singleton drives demo API views; instantiate a new simulator for isolated tests.
operational_simulator = OperationalSimulator(auto_advance=True)
