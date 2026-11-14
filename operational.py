"""Deterministic Mode-1 elevator *test stub* and direction-aware dispatch.

This is a software-only demonstration behind the logical elevator interface. It
is not physical equipment telemetry, an emergency controller or a safety system.
The state is process-local and must not be used as a production data store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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
    kind: Literal["HALL", "CAR", "SERVICE"]
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

    def reset(self) -> None:
        with self._lock:
            self.cars: dict[str, Car] = {eid: Car(eid) for eid in self.ELEVATOR_IDS}
            self.hall_calls: dict[str, HallRequest] = {}
            self.car_calls: dict[str, CarRequest] = {}
            self.sim_tick = 0
            self.base_time = datetime.now(timezone.utc)
            self._wall_reference = time.monotonic()

    def _now(self) -> datetime:
        return self.base_time + timedelta(seconds=self.sim_tick)

    @staticmethod
    def _valid_floor(elevator_id: str, floor: int) -> bool:
        return type(floor) is int and (1 <= floor <= 30 or (floor == -1 and elevator_id != "E5"))

    def _eligible(self, car: Car) -> bool:
        return (car.connected and car.availability == "IN_SERVICE"
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
        request.assigned_car = chosen.elevator_id
        request.transition("ASSIGNED")

    def _reassign_pending(self) -> None:
        # Reassignment occurs after a car has been isolated and also when an
        # eligible car returns while calls were queued.
        for request in self.hall_calls.values():
            if request.status == "QUEUED" and request.assigned_car is None:
                self._assign_request(request)

    def submit_hall_call(self, *, origin_floor: int, direction: HallDirection | None = None,
                         call_type: CallType = "ORDINARY") -> tuple[dict, bool]:
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

            request = HallRequest(str(uuid4()), origin_floor, call_type, direction, self._now())
            self.hall_calls[request.request_id] = request
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
            request = CarRequest(str(uuid4()), elevator_id, destination_floor, self._now())
            self.car_calls[request.request_id] = request
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
            request = CarRequest(str(uuid4()), "E5", destination_floor, self._now())
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
            else:
                request = self.car_calls[stop.request_id]
                request.status = "BLOCKED/REQUIRES_RECONCILIATION"
        car.stops.clear()
        car.direction = "IDLE"

    def isolate(self, elevator_id: str, availability: CarAvailability) -> None:
        """Trusted equipment-stub or authorized staff transition, not public passenger input."""
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if availability not in ("IN_SERVICE", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"):
                raise OperationalError("Unsupported operational state")
            if availability != "IN_SERVICE":
                self._release_car_hall_calls(car)
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
                    and car.door_state != "OBSTRUCTED" and car.availability not in ("FAULT", "STALE", "EMERGENCY_UNAVAILABLE"))

    def _serve_stop(self, car: Car) -> None:
        stop = car.stops.pop(0)
        car.door_state = "OPEN"
        car.dwell_remaining = 2
        car.direction = "IDLE"
        if stop.kind == "HALL":
            request = self.hall_calls[stop.request_id]
            request.transition("ARRIVED")
            request.arrived_at = self._now()
            request.wait_seconds = (request.arrived_at - request.accepted_at).total_seconds()
            request.transition("COMPLETED")
        else:
            request = self.car_calls[stop.request_id]
            request.status = "COMPLETED"
            request.completed_at = self._now()
            if stop.kind == "SERVICE":
                car.availability = "IN_SERVICE"

    def _advance_locked(self, seconds: int) -> None:
        for _ in range(seconds):
            self.sim_tick += 1
            for car in self.cars.values():
                if car.connected:
                    car.last_seen_tick = self.sim_tick  # test stub heartbeat, not physical telemetry
                if car.availability not in ("IN_SERVICE", "OCCUPIED"):
                    continue
                if car.dwell_remaining:
                    car.dwell_remaining -= 1
                    if not car.dwell_remaining:
                        car.door_state = "CLOSED"
                    continue
                if not car.stops:
                    car.direction = "IDLE"
                    continue
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
                    "message": "Software test-stub state; not connected to physical elevator equipment.",
                } for car in self.cars.values()],
            }


# The singleton drives demo API views; instantiate a new simulator for isolated tests.
operational_simulator = OperationalSimulator(auto_advance=True)
