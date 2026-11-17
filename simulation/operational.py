"""Deterministic Mode-1 elevator *test stub* and direction-aware dispatch.

This is a software-only demonstration behind the logical elevator interface. It
is not physical equipment telemetry, an emergency controller or a safety system.
The state is process-local and must not be used as a production data store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import os
from statistics import mean, median
import threading
import time
from typing import Literal
from uuid import uuid4
from db import DbConnectionPool
import asyncio

CarAvailability = Literal["IN_SERVICE", "OCCUPIED", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"]
HallDirection = Literal["UP", "DOWN"]
CallType = Literal["ORDINARY", "FREIGHT"]
TERMINAL_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}
CAR_TERMINAL_STATUSES = {"COMPLETED", "REJECTED", "CANCELLED", "FAILED"}
DEFAULT_BACKLOG_LIMIT = 100


def classify_traffic_profile(ts: datetime) -> str:
    """Classify a timestamp into PEAK, OFF_PEAK, or NORMAL using SRS PERF-8 default windows."""
    utc_ts = _normalize_utc(ts)
    hour = utc_ts.hour
    if (7 <= hour < 9) or (17 <= hour < 20):
        return "PEAK"
    if hour >= 22 or hour < 5:
        return "OFF_PEAK"
    return "NORMAL"


class OperationalError(ValueError):
    """A request violates operational dispatch or floor constraints."""


def _position(floor: int) -> int:
    """B1=-1 is adjacent to 1 (there is no Floor 0)."""
    return 0 if floor == -1 else floor


def _floor_from_position(position: int) -> int:
    return -1 if position == 0 else position


def _normalize_utc(ts: datetime) -> datetime:
    """Normalize naive or tz-aware datetimes to offset-aware UTC to match SimulationClock and TIMESTAMPTZ."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


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
    cancelled_by: str | None = None
    cancellation_reason: str | None = None
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
    acknowledged_at: datetime | None = None
    history: list[str] = field(default_factory=lambda: ["OPEN"])


class OperationalSimulator:
    """Thread-safe, single-process in-memory simulator; each simulated second is 1 floor step.

    Request state is kept in memory. Normal calls use E1-E4, freight calls E5.
    Only an ACS-authorized caller should invoke submit_car_call/start_service_run.
    """

    ELEVATOR_IDS = ("E1", "E2", "E3", "E4", "E5")

    def __init__(
        self,
        *,
        auto_advance: bool = False,
        load_from_db: bool = True,
        backlog_limit: int | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._auto_advance = auto_advance
        env_limit = os.getenv("SREMS_BACKLOG_LIMIT")
        resolved_limit = (
            backlog_limit
            if backlog_limit is not None
            else (int(env_limit) if env_limit and env_limit.isdigit() else DEFAULT_BACKLOG_LIMIT)
        )
        self.backlog_limit = max(1, resolved_limit)
        self.reset()
        if load_from_db:
            self._load_from_db_on_init()

    def _load_from_db_on_init(self) -> None:
        """Load persisted operational state if `DbConnectionPool` is already initialized."""
        import asyncio
        import os

        try:
            from db import DEFAULT_DSN, DbConnectionPool
        except Exception:
            return

        if DbConnectionPool._pool is None:
            return

        async def _connect_and_load() -> None:
            import asyncpg

            dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
            conn = await asyncpg.connect(dsn=dsn, timeout=1.0)
            try:
                await self.load_from_db(conn)
            finally:
                await conn.close()

        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        try:
            if in_running_loop:
                err: list[BaseException] = []

                def _worker() -> None:
                    try:
                        asyncio.run(_connect_and_load())
                    except BaseException as exc:
                        err.append(exc)

                thread = threading.Thread(target=_worker, daemon=True)
                thread.start()
                thread.join(timeout=2.0)
            else:
                asyncio.run(_connect_and_load())
        except Exception as exc:
            import logging
            logging.getLogger("srems.sync").debug("Init DB load skipped or failed: %s", exc)

    def reset(self, *, base_time: datetime | None = None, backlog_limit: int | None = None) -> None:
        with self._lock:
            if backlog_limit is not None:
                self.backlog_limit = max(1, backlog_limit)
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
            self._backlog_alerted = False
            self.sim_tick = 0
            self.base_time = (
                _normalize_utc(base_time)
                if base_time is not None
                else datetime.now(timezone.utc)
            )
            self._wall_reference = time.monotonic()
            self._synced_trips_idx = 0
            self._synced_downtime_idx = 0
            self._synced_closed_downtime_ids: set[str] = set()
            self._synced_heartbeats_idx = 0
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

            now = self._now()
            resolved_profile = traffic_profile or classify_traffic_profile(now)
            request = HallRequest(
                str(uuid4()),
                origin_floor,
                call_type,
                direction,
                now,
                traffic_profile=resolved_profile,
            )
            self.hall_calls[request.request_id] = request
            self._event("HALL_CALL", None, f"{call_type} hall call at {origin_floor}")
            request.transition("QUEUED")
            self._assign_request(request)
            self._check_backlog_event()
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
            self._check_backlog_event()
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

    def _clear_rescue_assignment(self, elevator_id: str, history_tag: str) -> None:
        for incident in self.incidents.values():
            if (
                incident.rescue_car_id == elevator_id
                and incident.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
            ):
                incident.rescue_car_id = None
                incident.history.append(history_tag)
                self._event(history_tag, elevator_id, incident.incident_id)

    def _release_car_hall_calls(self, car: Car) -> None:
        was_rescue = car.rescue_active
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
        if was_rescue:
            self._clear_rescue_assignment(car.elevator_id, "RESCUE_CANCELLED")

    def _close_open_downtime(
        self, elevator_id: str, now: datetime, *, technician_id: str | None = None
    ) -> None:
        for interval in self.downtime_intervals:
            if interval["elevator_id"] == elevator_id and interval["ended_at"] is None:
                interval["ended_at"] = now
                interval["duration_seconds"] = max(
                    0.0, (now - interval["started_at"]).total_seconds()
                )
                if technician_id and interval.get("technician_id") is None:
                    interval["technician_id"] = technician_id

    def isolate(
        self,
        elevator_id: str,
        availability: CarAvailability,
        *,
        technician_id: str | None = None,
    ) -> None:
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
                if any(
                    i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                    and (
                        i.kind in ("FIRE", "POWER_FAILURE")
                        or (
                            i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                            and i.elevator_id == elevator_id
                        )
                    )
                    for i in self.incidents.values()
                ):
                    raise OperationalError("Unresolved incident prevents return to service")
            now = self._now()
            if availability != "IN_SERVICE":
                self._release_car_hall_calls(car)
                self._event("ISOLATION", elevator_id, availability)
                if car.availability != availability:
                    self._close_open_downtime(elevator_id, now, technician_id=technician_id)
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
                            "technician_id": technician_id,
                        }
                    )
            else:
                self._close_open_downtime(elevator_id, now, technician_id=technician_id)
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
                    "traffic_profile": classify_traffic_profile(dep_time),
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
                            self._clear_rescue_assignment(car.elevator_id, "RESCUE_EXPIRED")
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
                start_tick = self.sim_tick
                self.sim_tick += seconds
                for car in self.cars.values():
                    if car.connected:
                        car.last_seen_tick = self.sim_tick
                first_hb = ((start_tick // 10) + 1) * 10
                for hb_tick in range(first_hb, self.sim_tick + 1, 10):
                    self.heartbeats.append(
                        {
                            "timestamp": self.base_time + timedelta(seconds=hb_tick),
                            "status": "UP",
                        }
                    )
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

    def _backlog_status(self) -> dict[str, int | bool]:
        active_hall = sum(
            1 for r in self.hall_calls.values() if r.status not in TERMINAL_STATUSES
        )
        active_car = sum(
            1 for c in self.car_calls.values() if c.status not in CAR_TERMINAL_STATUSES
        )
        active_requests = active_hall + active_car
        exceeded = active_requests > self.backlog_limit
        return {
            "active_hall_calls": active_hall,
            "active_car_calls": active_car,
            "active_requests": active_requests,
            "backlog_limit": self.backlog_limit,
            "backlog_exceeded": exceeded,
            "backlog_count": max(0, active_requests - self.backlog_limit),
        }

    def _check_backlog_event(self) -> None:
        status = self._backlog_status()
        if status["backlog_exceeded"]:
            if not self._backlog_alerted:
                self._backlog_alerted = True
                self._event(
                    "BACKLOG_EXCEEDED",
                    None,
                    f"{status['active_requests']} active requests exceed backlog limit {self.backlog_limit}",
                )
        else:
            self._backlog_alerted = False

    def fleet_status(self) -> dict:
        with self._lock:
            self._update_auto()
            backlog = self._backlog_status()
            return {
                "source": "labelled-test-stub",
                "sim_tick": self.sim_tick,
                "active_requests": backlog["active_requests"],
                "backlog_limit": backlog["backlog_limit"],
                "backlog_exceeded": backlog["backlog_exceeded"],
                "backlog_count": backlog["backlog_count"],
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

    def cancel_hall_call(
        self, request_id: str, actor: str, *, reason: str = "ADMIN_CANCELLED"
    ) -> dict:
        with self._lock:
            self._update_auto()
            request = self.hall_calls.get(request_id)
            if request is None or request.status in TERMINAL_STATUSES:
                raise OperationalError("Unknown or terminal hall call")
            if request.assigned_car:
                car = self.cars[request.assigned_car]
                car.stops = [stop for stop in car.stops if stop.request_id != request_id]
            request.assigned_car = None
            request.cancelled_by = actor
            request.cancellation_reason = reason
            request.transition("CANCELLED")
            self._event("HALL_CANCELLED", None, f"{request_id} by {actor}: {reason}")
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

    def confirm_help_as_stuck(self, incident_id: str, *, actor: str | None = None) -> dict:
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if incident.kind != "HELP" or incident.status in ("RESOLVED", "CLOSED", "FALSE_ALARM"):
                raise OperationalError("Only an open HELP request can be confirmed as stuck")
            incident.kind = "STUCK_CAR"
            incident.confirmed = True
            incident.history.append("CONFIRMED_STUCK")
            self.isolate(incident.elevator_id, "EMERGENCY_UNAVAILABLE", technician_id=actor)
            return asdict(incident)

    def building_emergency(self, kind: str, *, actor: str | None = None) -> dict:
        """Inject building fire or power status; default policy commands NO recall."""
        with self._lock:
            self._update_auto()
            if kind not in ("FIRE", "POWER_FAILURE"):
                raise OperationalError("Building event must be FIRE or POWER_FAILURE")
            incident = self._create_incident(kind, None,
                "Simulated building-wide event. Ordinary dispatch suspended; no recall.", "CRITICAL")
            for elevator_id in self.ELEVATOR_IDS:
                self.isolate(elevator_id, "EMERGENCY_UNAVAILABLE", technician_id=actor)
                self.cars[elevator_id].message = f"{kind} reported: wait for staff instructions"
            return asdict(incident)

    def report_fault(
        self, elevator_id: str, fault_code: str, *, actor: str | None = None
    ) -> dict:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if not fault_code.strip() or len(fault_code) > 80:
                raise OperationalError("Fault code must contain 1..80 characters")
            car.active_fault = fault_code.strip()
            if car.availability != "EMERGENCY_UNAVAILABLE":
                self.isolate(elevator_id, "FAULT", technician_id=actor)
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
            if car.rescue_active and action == "CLOSE":
                raise OperationalError("Door close prohibited during active rescue wait")
            car.door_state = "OPEN" if action == "OPEN" else "CLOSED"
            if car.rescue_active and action == "OPEN":
                car.dwell_remaining = max(car.dwell_remaining, car.rescue_wait_remaining)
            else:
                car.dwell_remaining = 2 if action == "OPEN" else 0
            self._event("DOOR_COMMAND", elevator_id, action)
            return self.fleet_status()

    def communication_loss(self, elevator_id: str, *, actor: str | None = None) -> dict:
        with self._lock:
            self._update_auto()
            self.isolate(elevator_id, "STALE", technician_id=actor)
            return asdict(self._create_incident("COMMUNICATION_LOSS", elevator_id,
                "Connection lost; outstanding commands require reconciliation"))

    def can_reconcile_connection(self, elevator_id: str) -> None:
        with self._lock:
            self._update_auto()
            car = self._car(elevator_id)
            if car.availability != "STALE":
                raise OperationalError("Car is not marked STALE")
            if car.active_fault or car.door_state == "OBSTRUCTED":
                raise OperationalError("Cannot reconcile with active fault or obstruction")

    def reconcile_connection(self, elevator_id: str, *, actor: str | None = None) -> dict:
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
            self.isolate(elevator_id, "IN_SERVICE", technician_id=actor)
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

    def select_rescue_candidate(
        self,
        incident_id: str,
        *,
        wait_seconds: int = 120,
        confirmed_empty: bool = False,
        preferred_car_id: str | None = None,
    ) -> tuple[str, int]:
        """Validate rescue preconditions and select the candidate rescue car without mutating state."""
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if incident.kind not in ("MEDICAL", "STUCK_CAR") or incident.status in ("RESOLVED", "CLOSED", "FALSE_ALARM"):
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
            if preferred_car_id is not None:
                return preferred_car_id, floor
            candidates = [
                c for c in list(self.cars.values())[:4]
                if c.elevator_id != incident.elevator_id and self._eligible(c)
                and all(s.kind == "HALL" for s in c.stops)
            ]
            if not candidates:
                raise OperationalError("No eligible, confirmed-empty rescue car")
            car = min(
                candidates,
                key=lambda c: (self._eta_and_insertion(c, floor, None)[0], int(c.elevator_id[1:])),
            )
            return car.elevator_id, floor

    def dispatch_rescue(
        self,
        incident_id: str,
        *,
        wait_seconds: int = 120,
        confirmed_empty: bool = False,
        rescue_car_id: str | None = None,
    ) -> dict:
        with self._lock:
            self._update_auto()
            chosen_id, floor = self.select_rescue_candidate(
                incident_id,
                wait_seconds=wait_seconds,
                confirmed_empty=confirmed_empty,
                preferred_car_id=rescue_car_id,
            )
            incident = self._incident(incident_id)
            car = self._car(chosen_id)
            if chosen_id == "E5" or chosen_id == incident.elevator_id or not self._eligible(car) or not all(s.kind == "HALL" for s in car.stops):
                raise OperationalError("Selected rescue car is ineligible or busy")
            self._release_car_hall_calls(car)
            car.rescue_active = True
            car.rescue_wait_remaining = wait_seconds
            car.stops.append(Stop(floor, "RESCUE", incident.incident_id))
            incident.rescue_car_id = car.elevator_id
            self._event("RESCUE_DISPATCH", car.elevator_id, f"Incident {incident_id}, wait {wait_seconds}s")
            self._reassign_pending()
            return {"incident": asdict(incident), "rescue_car_id": car.elevator_id,
                    "wait_seconds": wait_seconds}

    def cancel_rescue(self, incident_id: str, *, actor: str = "ADMIN") -> dict:
        """Admin cancels an active rescue-car dispatch/hold; car returns to normal dispatch and incident stays open."""
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            if not incident.rescue_car_id:
                raise OperationalError("Incident has no active rescue car")
            car = self._car(incident.rescue_car_id)
            car.stops = [s for s in car.stops if not (s.kind == "RESCUE" and s.request_id == incident_id)]
            car.rescue_active = False
            car.rescue_wait_remaining = 0
            car.dwell_remaining = 0
            car.door_state = "CLOSED"
            car.direction = "IDLE"
            car.message = "Rescue cancelled by Admin; returned to normal dispatch"
            released_car = car.elevator_id
            incident.rescue_car_id = None
            self._event("RESCUE_RELEASE", released_car, f"Cancelled by {actor} for {incident_id}")
            self._reassign_pending()
            return asdict(incident)

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
            incident.history.append("RESCUE_BOARDED")
            self._event("RESCUE_BOARDING", car.elevator_id, "Medical responder-floor run")
            return asdict(incident)

    def preview_incident_action(
        self,
        incident_id: str,
        action: str,
        *,
        technician_confirmation: bool = False,
    ) -> tuple[dict, list[str]]:
        """Validate incident action preconditions without mutating state and return cars that would return to service."""
        with self._lock:
            self._update_auto()
            incident = self._incident(incident_id)
            returning_cars: list[str] = []
            if action == "ACKNOWLEDGE":
                if incident.status != "OPEN":
                    raise OperationalError("Only OPEN incidents can be acknowledged")
            elif action in ("RESOLVE", "FALSE_ALARM"):
                if incident.status not in ("OPEN", "ACKNOWLEDGED"):
                    raise OperationalError("Incident already resolved or closed")
                if action == "RESOLVE" and incident.kind != "HELP" and not technician_confirmation:
                    raise OperationalError("Recovery confirmation is required")
                if incident.elevator_id:
                    car = self.cars[incident.elevator_id]
                    if car.rescue_active:
                        raise OperationalError("Rescue car still active")
                    if action == "RESOLVE" and car.availability in ("EMERGENCY_UNAVAILABLE", "FAULT", "MAINTENANCE", "STALE"):
                        if not self.recoverable(incident.elevator_id):
                            raise OperationalError("Recovery checks not satisfied")
                        if not any(
                            i is not incident and i.elevator_id == incident.elevator_id
                            and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                            and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                            for i in self.incidents.values()
                        ):
                            returning_cars.append(incident.elevator_id)
                elif incident.kind in ("FIRE", "POWER_FAILURE"):
                    if not any(
                        i is not incident and i.kind in ("FIRE", "POWER_FAILURE")
                        and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                        for i in self.incidents.values()
                    ):
                        for car in self.cars.values():
                            if car.availability == "EMERGENCY_UNAVAILABLE" and self.recoverable(car.elevator_id):
                                if not any(
                                    i is not incident and i.elevator_id == car.elevator_id
                                    and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                                    and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                                    for i in self.incidents.values()
                                ):
                                    returning_cars.append(car.elevator_id)
            elif action == "CLOSE":
                if incident.status != "RESOLVED":
                    raise OperationalError("Resolve before closing")
            else:
                raise OperationalError("Action must be ACKNOWLEDGE, RESOLVE, CLOSE or FALSE_ALARM")
            return asdict(incident), returning_cars

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
                incident.acknowledged_at = self._now()
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
                               and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM") for i in self.incidents.values()):
                        for car in self.cars.values():
                            if car.availability == "EMERGENCY_UNAVAILABLE":
                                if self.recoverable(car.elevator_id) and not any(
                                    i is not incident and i.elevator_id == car.elevator_id
                                    and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                                    and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                                    for i in self.incidents.values()
                                ):
                                    self.isolate(car.elevator_id, "IN_SERVICE", technician_id=actor)
                elif incident.elevator_id:
                    car = self.cars[incident.elevator_id]
                    if car.availability in ("EMERGENCY_UNAVAILABLE", "FAULT", "MAINTENANCE"):
                        if not any(i is not incident and i.elevator_id == incident.elevator_id
                                   and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                                   and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                                   for i in self.incidents.values()):
                            self.isolate(incident.elevator_id, "IN_SERVICE", technician_id=actor)
            elif action == "FALSE_ALARM":
                if incident.status not in ("OPEN", "ACKNOWLEDGED"):
                    raise OperationalError("Incident already resolved or closed")
                if incident.elevator_id and self.cars[incident.elevator_id].rescue_active:
                    raise OperationalError("Rescue car still active")
                incident.status = "FALSE_ALARM"
                incident.resolved_at = self._now()
                incident.resolved_by = actor
                if incident.elevator_id:
                    car = self.cars[incident.elevator_id]
                    if car.availability in ("EMERGENCY_UNAVAILABLE", "FAULT", "MAINTENANCE") and self.recoverable(incident.elevator_id):
                        if not any(
                            i is not incident and i.elevator_id == incident.elevator_id
                            and i.kind in ("MEDICAL", "STUCK_CAR", "BLOCKING_FAULT")
                            and i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")
                            for i in self.incidents.values()
                        ):
                            self.isolate(incident.elevator_id, "IN_SERVICE", technician_id=actor)
            elif action == "CLOSE":
                if incident.status != "RESOLVED":
                    raise OperationalError("Resolve before closing")
                incident.status = "CLOSED"
            else:
                raise OperationalError("Action must be ACKNOWLEDGE, RESOLVE, CLOSE or FALSE_ALARM")
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
            active_incidents = [i for i in self.incidents.values() if i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")]
            backlog = self._backlog_status()
            return {"source": "labelled-test-stub", "sim_tick": self.sim_tick,
                    "total_hall_calls": len(self.hall_calls), "completed_hall_calls": len(waits),
                    "active_hall_calls": backlog["active_hall_calls"],
                    "active_car_calls": backlog["active_car_calls"],
                    "active_requests": backlog["active_requests"],
                    "backlog_limit": backlog["backlog_limit"],
                    "backlog_exceeded": backlog["backlog_exceeded"],
                    "backlog_count": backlog["backlog_count"],
                    "mean_wait_seconds": mean(waits) if waits else None,
                    "median_wait_seconds": median(waits) if waits else None,
                    "p95_wait_seconds": pct(.95),
                    "completed_car_calls": len(completed_trips),
                    "active_incidents": len(active_incidents),
                    "fleet_available": sum(self._eligible(c) for c in self.cars.values()),
                    "fleet_count": len(self.cars), "events_count": len(self.events)}

    async def sync_to_db(self, conn: object) -> None:
        """Asynchronously sync operational state (hall_calls, trips, car_calls, incidents, heartbeats, downtime) to PostgreSQL."""
        if conn is None:
            return
        try:
            with self._lock:
                hall_calls_snapshot = list(self.hall_calls.values())
                car_calls_snapshot = list(self.car_calls.values())
                trips_to_sync = list(self.trips[self._synced_trips_idx:])
                incidents_snapshot = list(self.incidents.values())
                downtime_to_sync = [
                    dict(di)
                    for idx, di in enumerate(self.downtime_intervals)
                    if idx >= self._synced_downtime_idx
                    or di["interval_id"] not in self._synced_closed_downtime_ids
                ]
                heartbeats_to_sync = list(self.heartbeats[self._synced_heartbeats_idx:])
                current_trips_len = len(self.trips)
                current_downtime_len = len(self.downtime_intervals)
                current_heartbeats_len = len(self.heartbeats)

            def _aware(dt: datetime | None) -> datetime | None:
                return _normalize_utc(dt) if dt is not None else None

            # 1. Hall Calls
            for r in hall_calls_snapshot:
                valid_status = r.status if r.status in ('RECEIVED', 'QUEUED', 'ASSIGNED', 'APPROACHING', 'ARRIVED', 'COMPLETED', 'CANCELLED', 'FAILED', 'BLOCKED') else 'QUEUED'
                accepted_at = _aware(r.accepted_at)
                arrived_at = _aware(r.arrived_at)
                await conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO hall_calls (
                        call_id, origin_floor, direction, is_freight, assigned_elevator_id,
                        status, created_at, assigned_at, approaching_at, arrived_at, completed_at,
                        wait_time_seconds, reassignments_count, no_boarding, cancellation_reason, traffic_profile
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    ON CONFLICT (call_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        assigned_elevator_id = EXCLUDED.assigned_elevator_id,
                        arrived_at = EXCLUDED.arrived_at,
                        completed_at = EXCLUDED.completed_at,
                        wait_time_seconds = EXCLUDED.wait_time_seconds,
                        reassignments_count = EXCLUDED.reassignments_count,
                        no_boarding = EXCLUDED.no_boarding,
                        cancellation_reason = EXCLUDED.cancellation_reason;
                    """,
                    r.request_id,
                    r.origin_floor,
                    r.direction or "UP",
                    r.call_type == "FREIGHT",
                    r.assigned_car,
                    valid_status,
                    accepted_at,
                    accepted_at if r.assigned_car else None,
                    arrived_at if r.status in ('APPROACHING', 'ARRIVED', 'COMPLETED') else None,
                    arrived_at,
                    arrived_at if r.status == 'COMPLETED' else None,
                    r.wait_seconds,
                    r.reassignment_count,
                    r.no_boarding,
                    r.cancellation_reason,
                    r.traffic_profile or "NORMAL"
                )

            # 2. Trips
            for t in trips_to_sync:
                await conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO trips (
                        trip_id, elevator_id, origin_floor, destination_floor,
                        departure_time, arrival_time, travel_time_seconds, floors_traversed,
                        motor_starts, traffic_profile, is_service_run, is_estimated
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (trip_id) DO NOTHING;
                    """,
                    t["trip_id"],
                    t["elevator_id"],
                    t["origin_floor"],
                    t["destination_floor"],
                    _aware(t["departure_time"]),
                    _aware(t["arrival_time"]),
                    float(t["travel_time_seconds"]),
                    t["floors_traversed"],
                    t.get("motor_starts", 1),
                    t.get("traffic_profile") or "NORMAL",
                    t.get("is_service_run", False),
                    t.get("is_estimated", False),
                )

            # 3. Car Calls
            for c in car_calls_snapshot:
                valid_car_status = c.status if c.status in ('RECEIVED', 'AUTHORIZATION_CHECK', 'QUEUED', 'IN_TRANSIT', 'COMPLETED', 'REJECTED', 'CANCELLED', 'FAILED', 'BLOCKED') else 'QUEUED'
                c_accepted = _aware(c.accepted_at)
                c_completed = _aware(c.completed_at)
                await conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO car_calls (
                        call_id, elevator_id, destination_floor, credential_id,
                        status, created_at, completed_at, travel_time_seconds, is_service_run, cancellation_reason
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (call_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        completed_at = EXCLUDED.completed_at,
                        travel_time_seconds = EXCLUDED.travel_time_seconds;
                    """,
                    c.request_id,
                    c.elevator_id,
                    c.destination_floor,
                    None,
                    valid_car_status,
                    c_accepted,
                    c_completed,
                    (c_completed - c_accepted).total_seconds() if c_completed and c_accepted else None,
                    c.is_service_run,
                    None
                )

            # 4. Incidents
            kind_map = {
                "HELP": "HELP_REQUEST",
                "MEDICAL": "MEDICAL_EMERGENCY",
                "STUCK_CAR": "STUCK_CAR",
                "FIRE": "FIRE",
                "POWER_FAILURE": "POWER_FAILURE",
                "DOOR_OBSTRUCTION": "DOOR_OBSTRUCTION",
                "COMMUNICATION_FAILURE": "COMMUNICATION_FAILURE",
                "BLOCKING_FAULT": "STUCK_CAR",
                "REPEATED_EMERGENCY": "REPEATED_EMERGENCY",
                "REPEATED_EMPTY_CALLS": "REPEATED_EMPTY_CALLS",
            }
            severity_map = {
                "LOW": "LOW",
                "MEDIUM": "MEDIUM",
                "HIGH": "HIGH",
                "CRITICAL": "CRITICAL",
                "WARNING": "MEDIUM",
            }
            status_map = {
                "OPEN": "OPEN",
                "ACKNOWLEDGED": "ACKNOWLEDGED",
                "RESOLVED": "RESOLVED",
                "CLOSED": "CLOSED",
                "FALSE_ALARM": "FALSE_ALARM",
            }
            for inc in incidents_snapshot:
                await conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO incidents (
                        incident_id, incident_type, elevator_id, floor, status, severity,
                        details, created_at, acknowledged_at, resolved_at, resolved_by, rescue_car_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (incident_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        acknowledged_at = COALESCE(EXCLUDED.acknowledged_at, incidents.acknowledged_at),
                        resolved_at = EXCLUDED.resolved_at,
                        resolved_by = EXCLUDED.resolved_by,
                        rescue_car_id = EXCLUDED.rescue_car_id;
                    """,
                    inc.incident_id,
                    kind_map.get(inc.kind, "HELP_REQUEST"),
                    inc.elevator_id,
                    inc.floor,
                    status_map.get(inc.status, "OPEN"),
                    severity_map.get(inc.severity, "LOW"),
                    inc.details,
                    _aware(inc.created_at),
                    _aware(inc.acknowledged_at),
                    _aware(inc.resolved_at),
                    inc.resolved_by,
                    inc.rescue_car_id,
                )

            # 5. Downtime
            state_map = {
                "FAULT": "FAULT",
                "MAINTENANCE": "MAINTENANCE",
                "EMERGENCY": "EMERGENCY",
                "EMERGENCY_UNAVAILABLE": "EMERGENCY_UNAVAILABLE",
                "STALE": "STALE",
            }
            for di in downtime_to_sync:
                await conn.execute(  # type: ignore[union-attr]
                    """
                    INSERT INTO car_downtime_intervals (
                        interval_id, elevator_id, state, cause, started_at, ended_at,
                        duration_seconds, is_ongoing, technician_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (interval_id) DO UPDATE SET
                        ended_at = EXCLUDED.ended_at,
                        duration_seconds = EXCLUDED.duration_seconds,
                        is_ongoing = EXCLUDED.is_ongoing,
                        technician_id = COALESCE(EXCLUDED.technician_id, car_downtime_intervals.technician_id);
                    """,
                    di["interval_id"],
                    di["elevator_id"],
                    state_map.get(di["state"], "FAULT"),
                    di.get("reason"),
                    _aware(di["started_at"]),
                    _aware(di.get("ended_at")),
                    di.get("duration_seconds"),
                    di.get("ended_at") is None,
                    di.get("technician_id"),
                )

            # 6. Heartbeats
            if heartbeats_to_sync:
                active_elevators = sum(self._eligible(c) for c in self.cars.values())
                active_requests = sum(r.status not in TERMINAL_STATUSES for r in self.hall_calls.values())
                active_incidents = sum(i.status not in ("RESOLVED", "CLOSED") for i in self.incidents.values())
                hb_sql = """
                    INSERT INTO system_heartbeats (
                        timestamp, status, active_elevators_count, active_requests_count, active_incidents_count
                    )
                    VALUES ($1, $2, $3, $4, $5);
                    """
                hb_rows = [
                    (
                        _aware(hb["timestamp"]),
                        hb.get("status", "UP"),
                        active_elevators,
                        active_requests,
                        active_incidents,
                    )
                    for hb in heartbeats_to_sync
                ]
                if hasattr(conn, "executemany"):
                    await conn.executemany(hb_sql, hb_rows)  # type: ignore[union-attr]
                else:
                    for row in hb_rows:
                        await conn.execute(hb_sql, *row)  # type: ignore[union-attr]

            with self._lock:
                self._synced_trips_idx = current_trips_len
                self._synced_downtime_idx = current_downtime_len
                self._synced_closed_downtime_ids.update(
                    di["interval_id"]
                    for di in downtime_to_sync
                    if di.get("ended_at") is not None
                )
                self._synced_heartbeats_idx = current_heartbeats_len
        except Exception as exc:
            import logging
            logging.getLogger("srems.sync").debug("DB sync skipped or failed: %s", exc)

    async def load_from_db(self, conn: object) -> bool:
        """Load operational state previously persisted by `sync_to_db` from PostgreSQL.

        Returns True if any operational rows were loaded, False otherwise.
        """
        if conn is None:
            return False
        try:
            hall_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT call_id, origin_floor, direction, is_freight, assigned_elevator_id,
                           status, created_at, arrived_at, wait_time_seconds,
                           reassignments_count, no_boarding, traffic_profile
                    FROM hall_calls
                    ORDER BY created_at ASC
                    """
                )
            ]
            trip_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT trip_id, elevator_id, origin_floor, destination_floor,
                           departure_time, arrival_time, travel_time_seconds,
                           floors_traversed, motor_starts, traffic_profile,
                           is_service_run, is_estimated
                    FROM trips
                    ORDER BY departure_time ASC, arrival_time ASC
                    """
                )
            ]
            car_call_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT call_id, elevator_id, destination_floor, status,
                           created_at, completed_at, is_service_run
                    FROM car_calls
                    ORDER BY created_at ASC
                    """
                )
            ]
            incident_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT incident_id, incident_type, elevator_id, floor, status,
                           severity, details, created_at, resolved_at, resolved_by, rescue_car_id
                    FROM incidents
                    ORDER BY created_at ASC
                    """
                )
            ]
            downtime_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT interval_id, elevator_id, state, cause, started_at, ended_at,
                           duration_seconds, technician_id
                    FROM car_downtime_intervals
                    ORDER BY started_at ASC
                    """
                )
            ]
            heartbeat_rows = [
                dict(r)
                for r in await conn.fetch(  # type: ignore[union-attr]
                    """
                    SELECT timestamp, status
                    FROM system_heartbeats
                    ORDER BY timestamp ASC, heartbeat_id ASC
                    """
                )
            ]
        except Exception as exc:
            import logging
            logging.getLogger("srems.sync").debug("DB load skipped or failed: %s", exc)
            return False

        if not (hall_rows or trip_rows or car_call_rows or incident_rows or downtime_rows or heartbeat_rows):
            return False

        reverse_kind_map = {
            "HELP_REQUEST": "HELP",
            "MEDICAL_EMERGENCY": "MEDICAL",
            "STUCK_CAR": "STUCK_CAR",
            "FIRE": "FIRE",
            "FIRE_ALARM": "FIRE",
            "POWER_FAILURE": "POWER_FAILURE",
            "DOOR_OBSTRUCTION": "DOOR_OBSTRUCTION",
            "COMMUNICATION_FAILURE": "COMMUNICATION_FAILURE",
            "COMMUNICATION_LOSS": "COMMUNICATION_LOSS",
            "REPEATED_EMERGENCY": "REPEATED_EMERGENCY",
            "REPEATED_EMPTY_CALLS": "REPEATED_EMPTY_CALLS",
        }

        with self._lock:
            self.reset()
            timestamps: list[datetime] = []

            # 1. Trips
            loaded_trips: list[dict] = []
            for row in trip_rows:
                eid = str(row["elevator_id"])
                dep_ts = _normalize_utc(row["departure_time"])
                arr_ts = _normalize_utc(row["arrival_time"])
                loaded_trips.append(
                    {
                        "trip_id": str(row["trip_id"]),
                        "elevator_id": eid,
                        "origin_floor": int(row["origin_floor"]),
                        "destination_floor": int(row["destination_floor"]),
                        "departure_time": dep_ts,
                        "arrival_time": arr_ts,
                        "travel_time_seconds": float(row["travel_time_seconds"]),
                        "floors_traversed": int(row["floors_traversed"]),
                        "motor_starts": int(row.get("motor_starts") or 1),
                        "is_estimated": bool(row.get("is_estimated", False)),
                        "is_service_run": bool(row.get("is_service_run", False)),
                        "traffic_profile": row.get("traffic_profile"),
                    }
                )
                if eid in self.cars:
                    self.cars[eid].current_floor = int(row["destination_floor"])
                timestamps.extend([dep_ts, arr_ts])
            self.trips = loaded_trips
            self._synced_trips_idx = len(self.trips)

            # 2. Hall calls
            for row in hall_rows:
                is_freight = bool(row.get("is_freight", False))
                status = str(row["status"])
                no_boarding = bool(row.get("no_boarding", False))
                assigned_car = row.get("assigned_elevator_id")
                accepted_ts = _normalize_utc(row["created_at"])
                arrived_ts = (
                    _normalize_utc(row["arrived_at"])
                    if row.get("arrived_at") is not None
                    else None
                )
                history = ["RECEIVED"]
                for step in ("QUEUED", "ASSIGNED", "APPROACHING", "ARRIVED", "COMPLETED"):
                    if step == status:
                        history.append(step)
                        break
                    if status in ("COMPLETED", "ARRIVED", "APPROACHING", "ASSIGNED"):
                        history.append(step)
                if status not in history:
                    history.append(status)

                req = HallRequest(
                    request_id=str(row["call_id"]),
                    origin_floor=int(row["origin_floor"]),
                    call_type="FREIGHT" if is_freight else "ORDINARY",
                    direction=None if is_freight else row.get("direction"),
                    accepted_at=accepted_ts,
                    status=status,
                    assigned_car=assigned_car,
                    arrived_at=arrived_ts,
                    wait_seconds=(
                        float(row["wait_time_seconds"])
                        if row.get("wait_time_seconds") is not None
                        else None
                    ),
                    reassignment_count=int(row.get("reassignments_count") or 0),
                    boarding_evidence=False if no_boarding else None,
                    no_boarding=no_boarding,
                    traffic_profile=row.get("traffic_profile"),
                    history=history,
                )
                self.hall_calls[req.request_id] = req
                timestamps.append(req.accepted_at)
                if req.arrived_at is not None:
                    timestamps.append(req.arrived_at)
                if status in ("ASSIGNED", "APPROACHING") and assigned_car in self.cars:
                    self.cars[assigned_car].stops.append(
                        Stop(req.origin_floor, "HALL", req.request_id)
                    )

            # 3. Car calls
            for row in car_call_rows:
                eid = str(row["elevator_id"])
                status = str(row["status"])
                is_service = bool(row.get("is_service_run", False))
                accepted_ts = _normalize_utc(row["created_at"])
                completed_ts = (
                    _normalize_utc(row["completed_at"])
                    if row.get("completed_at") is not None
                    else None
                )
                car_req = CarRequest(
                    request_id=str(row["call_id"]),
                    elevator_id=eid,
                    destination_floor=int(row["destination_floor"]),
                    accepted_at=accepted_ts,
                    status=status,
                    completed_at=completed_ts,
                    is_service_run=is_service,
                )
                self.car_calls[car_req.request_id] = car_req
                timestamps.append(car_req.accepted_at)
                if car_req.completed_at is not None:
                    timestamps.append(car_req.completed_at)
                if status in ("QUEUED", "IN_TRANSIT") and eid in self.cars:
                    stop_kind = "SERVICE" if is_service else "CAR"
                    self.cars[eid].stops.append(
                        Stop(car_req.destination_floor, stop_kind, car_req.request_id)
                    )
                    if is_service:
                        self.cars[eid].availability = "OCCUPIED"

            # 4. Incidents
            open_emergency_cars: set[str] = set()
            building_emergency_active = False
            for row in incident_rows:
                kind = reverse_kind_map.get(str(row["incident_type"]), str(row["incident_type"]))
                status = str(row["status"])
                created_ts = _normalize_utc(row["created_at"])
                resolved_ts = (
                    _normalize_utc(row["resolved_at"])
                    if row.get("resolved_at") is not None
                    else None
                )
                inc = Incident(
                    incident_id=str(row["incident_id"]),
                    kind=kind,
                    elevator_id=row.get("elevator_id"),
                    status=status,
                    severity=str(row.get("severity") or "LOW"),
                    created_at=created_ts,
                    details=str(row.get("details") or ""),
                    floor=row.get("floor"),
                    resolved_by=row.get("resolved_by"),
                    resolved_at=resolved_ts,
                    rescue_car_id=row.get("rescue_car_id"),
                    history=["OPEN"] if status == "OPEN" else ["OPEN", status],
                )
                self.incidents[inc.incident_id] = inc
                timestamps.append(inc.created_at)
                if inc.resolved_at is not None:
                    timestamps.append(inc.resolved_at)
                if status not in ("RESOLVED", "CLOSED", "FALSE_ALARM"):
                    if kind in ("FIRE", "POWER_FAILURE"):
                        building_emergency_active = True
                    elif kind in ("MEDICAL", "STUCK_CAR") and inc.elevator_id:
                        open_emergency_cars.add(inc.elevator_id)
                    if inc.rescue_car_id and inc.rescue_car_id in self.cars:
                        self.cars[inc.rescue_car_id].rescue_active = True
                    if kind == "DOOR_OBSTRUCTION" and inc.elevator_id in self.cars:
                        self.cars[inc.elevator_id].obstruction_alerted = True

            # 5. Downtime intervals
            loaded_downtime: list[dict] = []
            first_open_idx: int | None = None
            for idx, row in enumerate(downtime_rows):
                eid = str(row["elevator_id"])
                state = str(row["state"])
                reason = row.get("cause")
                started_ts = _normalize_utc(row["started_at"])
                ended_ts = (
                    _normalize_utc(row["ended_at"])
                    if row.get("ended_at") is not None
                    else None
                )
                loaded_downtime.append(
                    {
                        "interval_id": str(row["interval_id"]),
                        "elevator_id": eid,
                        "state": state,
                        "reason": reason,
                        "started_at": started_ts,
                        "ended_at": ended_ts,
                        "duration_seconds": (
                            float(row["duration_seconds"])
                            if row.get("duration_seconds") is not None
                            else None
                        ),
                        "technician_id": row.get("technician_id"),
                    }
                )
                timestamps.append(started_ts)
                if ended_ts is not None:
                    timestamps.append(ended_ts)
                else:
                    if first_open_idx is None:
                        first_open_idx = idx
                    if eid in self.cars:
                        car = self.cars[eid]
                        if reason == "COMMUNICATION_LOSS" or state == "STALE":
                            car.availability = "STALE"
                            car.connected = False
                        elif (
                            state in ("EMERGENCY", "EMERGENCY_UNAVAILABLE")
                            or reason == "EMERGENCY_UNAVAILABLE"
                            or building_emergency_active
                            or eid in open_emergency_cars
                        ):
                            car.availability = "EMERGENCY_UNAVAILABLE"
                            car.connected = True
                        elif state == "MAINTENANCE":
                            car.availability = "MAINTENANCE"
                            car.connected = True
                        else:
                            car.availability = "FAULT"
                            car.connected = True
                            if reason and reason != "FAULT":
                                car.active_fault = str(reason)

            if building_emergency_active:
                for car in self.cars.values():
                    if car.availability != "STALE":
                        car.availability = "EMERGENCY_UNAVAILABLE"

            self.downtime_intervals = loaded_downtime
            self._synced_closed_downtime_ids = {
                d["interval_id"]
                for d in loaded_downtime
                if d.get("ended_at") is not None
            }
            self._synced_downtime_idx = (
                first_open_idx if first_open_idx is not None else len(self.downtime_intervals)
            )

            # 6. Heartbeats
            now_utc = datetime.now(timezone.utc)
            self.base_time = now_utc
            self.sim_tick = 0
            if heartbeat_rows:
                self.heartbeats = [
                    {
                        "timestamp": _normalize_utc(row["timestamp"]),
                        "status": str(row["status"]),
                    }
                    for row in heartbeat_rows
                ]
                self._synced_heartbeats_idx = len(self.heartbeats)
                self.heartbeats.append({"timestamp": now_utc, "status": "UP"})
            else:
                self.heartbeats = [{"timestamp": now_utc, "status": "UP"}]
                self._synced_heartbeats_idx = 0

            for car in self.cars.values():
                if car.connected:
                    car.last_seen_tick = self.sim_tick

        return True


# The singleton drives demo API views; instantiate a new simulator for isolated tests.
operational_simulator = OperationalSimulator(auto_advance=True)
