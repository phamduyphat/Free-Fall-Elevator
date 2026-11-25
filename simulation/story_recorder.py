"""Record SRS validation scenarios from the real simulator for the scenario player.

Every story below drives ``OperationalSimulator`` (the same code the server runs) through
one or more SRS validation tests (VT-xx). For each simulated second the recorder stores a
frame (cars, doors, calls, people, incidents) and turns what the simulator did into
explained events: which module acted, what it decided and why, and which functions ran
(with their real file and line). The pass criteria of each test are predicates on the
simulator's state, evaluated every second; the player shows the moment each one passes.

Nothing in a story is a hard-coded outcome: change the dispatcher and the recording
changes. Access control (Mode 2, PostgreSQL) is not exercised here; destinations are
submitted the way the API does after ACS has granted them (see VT-02 tests for ACS).

Regenerate the player data (about 10 s)::

    python -m simulation.story_recorder --out frontend/public/player/stories.js
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
from typing import Any, Callable

from simulation import wear as wear_mod
from simulation.operational import (
    TERMINAL_STATUSES,
    OperationalError,
    OperationalSimulator,
)
from simulation.wear import WearConfig, WearTracker

REPO = Path(__file__).resolve().parent.parent
CAPACITY = {"E1": 13, "E2": 13, "E3": 13, "E4": 13, "E5": 20}
AVAIL_CODE = {"IN_SERVICE": "S", "OCCUPIED": "O", "MAINTENANCE": "M", "FAULT": "F",
              "EMERGENCY_UNAVAILABLE": "E", "STALE": "T"}
KIND_CODE = {"HALL": "H", "CAR": "C", "SERVICE": "V", "MEDICAL": "M", "RESCUE": "R"}
HALL_STATES = ["RECEIVED", "QUEUED", "ASSIGNED", "APPROACHING", "ARRIVED", "COMPLETED"]
INCIDENT_STATES = ["OPEN", "ACKNOWLEDGED", "RESOLVED", "CLOSED"]


def floor_name(f: int | None) -> str:
    return "—" if f is None else ("B1" if f == -1 else str(f))


# --------------------------------------------------------------------------- code refs
def _ref(fn: Callable, why: str) -> dict:
    src = Path(inspect.getsourcefile(fn) or "")
    try:
        rel = src.resolve().relative_to(REPO).as_posix()
    except ValueError:
        rel = src.name
    return {"fn": fn.__qualname__, "file": rel, "line": inspect.getsourcelines(fn)[1], "why": why}


S = OperationalSimulator
CODE: dict[str, list[dict]] = {
    "hall": [_ref(S.submit_hall_call, "Validates floor and direction, merges a repeat press into the active request (FR-1.22) and records it."),
             _ref(S._assign_request, "Chooses the car with the lowest cost: predicted service time plus wear penalty.")],
    "assign": [_ref(S._assign_request, "Compares every eligible car of the group and inserts the stop into the winner's route (FR-1.4, FR-1.5)."),
               _ref(S._eta_and_insertion, "Walks each car's committed route leg by leg; a floor on a leg going the right way is picked up on the way."),
               _ref(WearTracker.usage_penalties, "Adds at most 15 s to cars that travelled more this month than the least-used one, and only while the group is not busy (WEAR-3).")],
    "arrive": [_ref(S._advance_locked, "Moves each car one floor per simulated second; never with an obstruction or open doors."),
               _ref(S._serve_stop, "Opens the doors once for every stop at this floor and records the trip."),
               _ref(S._complete_passenger_stop, "Hall call ARRIVED then COMPLETED; wait = arrival − acceptance (FR-1.18); boarding window opens.")],
    "carcall": [_ref(S.submit_car_call, "Records the destination (RECEIVED → AUTHORIZATION_CHECK → QUEUED) after ACS granted it."),
                _ref(S._route_insert_index, "Collective control: the destination is served on the way when the route passes it.")],
    "merge": [_ref(S.submit_hall_call, "Same floor and direction already active: returns the existing request (FR-1.22).")],
    "incident": [_ref(S._create_incident, "Stores type, scope, floor, direction, trigger and related requests (EMR-3.4).")],
    "help": [_ref(S.trigger_emergency, "In-car HELP or MEDICAL; no card or role is checked (EMR-3.1)."),
             _ref(S._create_incident, "Stores type, scope, floor, direction, trigger and related requests (EMR-3.4).")],
    "medical": [_ref(S.trigger_emergency, "MEDICAL: car becomes EMERGENCY_UNAVAILABLE and gets a non-stop stop at floor 1 (EMR-3.14)."),
                _ref(S.isolate, "Takes the car out of dispatch; its hall calls go back to the queue, car calls are blocked (EMR-3.3).")],
    "stuck": [_ref(S.confirm_help_as_stuck, "Admin confirms a HELP request: it becomes a stuck-car emergency (EMR-3.1)."),
              _ref(S.isolate, "EMERGENCY_UNAVAILABLE: hall calls requeued, car calls blocked (EMR-3.3).")],
    "escalate": [_ref(S._escalate_incidents, "An incident still open after 60 s re-notifies security (EMR-3.9).")],
    "resolve": [_ref(S.incident_action, "Acknowledge / resolve / close; resolving needs the recovery confirmation."),
                _ref(S.recoverable, "Recovery checks (MNT-4.6): fresh state, no fault, doors not obstructed.")],
    "isolate": [_ref(S.isolate, "Changes availability; out of service releases the car's hall calls to the queue."),
                _ref(S._release_car_hall_calls, "Requeues hall calls (keeping the acceptance time) and blocks car calls.")],
    "return": [_ref(S.isolate, "IN_SERVICE only when the recovery checks pass and no incident is open (MNT-4.6)."),
               _ref(S.recoverable, "Recovery checks: connected, fresh, no fault, doors not obstructed.")],
    "fault": [_ref(S.report_fault, "Blocking fault: car isolated as FAULT and an incident opened (MNT-4.1).")],
    "obstruction": [_ref(S.door_obstruction, "Door sensor event: door state OBSTRUCTED until it clears (FR-1.15)."),
                    _ref(S._advance_locked, "Never moves a car with an active obstruction; opens an incident after 10 s.")],
    "door": [_ref(S.passenger_door_command, "Door command; CLOSE is rejected while an obstruction is active (FR-1.15).")],
    "comms": [_ref(S.communication_loss, "Marks the car STALE, records in-flight commands as UNKNOWN (MNT-4.8)."),
              _ref(S.isolate, "STALE: no new commands, hall calls requeued, car calls blocked.")],
    "reconcile": [_ref(S.reconcile_connection, "Fresh snapshot, unknown outcomes marked FAILED, never replayed (COM-4, MNT-4.9).")],
    "rescue": [_ref(S.select_rescue_candidate, "Eligible empty car by ETA; never the affected car, never E5 (EMR-3.12)."),
               _ref(S.dispatch_rescue, "Requeues the rescue car's hall calls and sends it to the floor with a door-hold time.")],
    "rescue_board": [_ref(S.confirm_rescue_boarding, "Admin confirms the patient is aboard: non-stop run to floor 1 (EMR-3.16).")],
    "phone": [_ref(S.phone_medical_incident, "Admin records a medical emergency reported by telephone (EMR-3.16).")],
    "fire": [_ref(S.building_emergency, "Building-wide policy: every car EMERGENCY_UNAVAILABLE, no recall movement (EMR-3.6).")],
    "service": [_ref(S.start_service_run, "E5 service run: OCCUPIED, exclusive, non-stop to its destination (FR-1.13).")],
    "noboard": [_ref(S._detect_empty_calls, "No boarding within 30 s → NO_BOARDING; five from one floor in 10 min → low alert (FR-1.21, FR-1.22).")],
    "evidence": [_ref(S.boarding_evidence, "Labelled test stub supplies boarding evidence (SWI-2).")],
    "cancel": [_ref(S.cancel_hall_call, "Only the system or an Admin may cancel; actor and reason recorded (FR-1.20).")],
    "wear": [_ref(S._roll_wear_windows, "At 00:00 on the 1st the month closes and is judged against 2 % (WEAR-4)."),
             _ref(wear_mod.WearTracker.advance_to, "Records per-car floors and spread, resets month counters; every 6 months the period counters.")],
    "load": [_ref(S.report_load, "Test-stub load sensor: people on board over rated capacity (FR-1.14, CAP-2)."),
             _ref(S._assign_request, "A full car is left out of the comparison; its reason shows as 'full load'.")],
    "bypass": [_ref(S._advance_locked, "Full-load bypass: a full car passes its pickups; they go back to the queue."),
               _ref(S._reassign_pending, "Queued calls are reassigned oldest first; acceptance time is kept (FR-1.18).")],
    "command": [_ref(S._record_command, "Every interface command ends ACKNOWLEDGED, REJECTED, TIMED_OUT or UNKNOWN (COM-3).")],
}


# --------------------------------------------------------------------------- recorder
class Person:
    __slots__ = ("pid", "origin", "dest", "state", "req", "car", "since", "exit_ttl", "label", "repress_at",
                 "kind", "start", "t_board")

    def __init__(self, pid: int, origin: int, dest: int, label: str | None, t: int) -> None:
        self.pid = pid
        self.origin = origin
        self.dest = dest
        self.state = "w"  # w = waiting at landing, r = riding, x = walking out
        self.req: str | None = None
        self.car: str | None = None
        self.since = t
        self.exit_ttl = 0
        self.label = label
        self.repress_at: int | None = None
        self.kind = "resident"      # or "freight" (E5 freight key)
        self.start = origin         # where the journey began (origin changes when walking out)
        self.t_board: int | None = None


class Story:
    """Script API used by the stories below."""

    def __init__(self, key: str, title: str, *, vt: list[str], mode: str, tagline: str,
                 requirements: list[str], base: datetime, note: str = "",
                 wear: WearConfig | None = None, period_start: datetime | None = None,
                 traffic_minor: bool = False) -> None:
        self.meta = {"key": key, "title": title, "vt": vt, "mode": mode, "tagline": tagline,
                     "requirements": requirements, "note": note,
                     "base": base.isoformat()}
        self.sim = OperationalSimulator(auto_advance=False, load_from_db=False,
                                        wear_config=wear or WearConfig())
        self.sim.reset(base_time=base)
        if period_start is not None:
            self.sim.wear = WearTracker(self.sim.ELEVATOR_IDS, base, self.sim.wear.config,
                                        period_start=period_start)
            self.sim._wear_boundary_tick = self.sim._tick_for(self.sim.wear.next_boundary())
        self.sim.decision_trace = []
        self.people: dict[int, Person] = {}
        self._next_pid = 1
        self.frames: list[dict] = []
        self.events: list[dict] = []
        self.criteria: list[dict] = []
        self.strings: list[str] = [""]
        self._seen_events = 0
        self._seen_cmds = 0
        self._decisions: dict[str, dict] = {}
        self._seen_decisions = 0
        self._avail = {eid: c.availability for eid, c in self.sim.cars.items()}
        self._reassign = {}
        self._caption: tuple[str, bool] | None = None
        self.labels: dict[str, str] = {}  # label -> hall request id
        self.served_floors: dict[str, list[int]] = {eid: [] for eid in self.sim.ELEVATOR_IDS}
        self.door_opens: dict[str, list[tuple[int, int]]] = {eid: [] for eid in self.sim.ELEVATOR_IDS}
        self._last_door = {eid: c.door_state for eid, c in self.sim.cars.items()}
        self.traffic_minor = traffic_minor
        self.flags: dict[str, Any] = {}
        self.journeys: list[dict] = []   # completed journeys: appear, board and arrival ticks
        self.started = False
        if period_start is None:
            # Realistic month-to-date trip counters for a balanced fleet (day 23 of the month);
            # every floor travelled during the story is added by the simulator itself.
            for eid in ("E1", "E2", "E3", "E4"):
                self.preset_counters(eid, month=465_000, period=1_550_000, lifetime=9_870_000)
            self.preset_counters("E5", month=78_000, period=262_000, lifetime=1_610_000)
            self.flags["counters_start"] = {"E1": 465_000, "E2": 465_000, "E3": 465_000, "E4": 465_000, "E5": 78_000}

    # ---------------------------------------------------------------- setup helpers
    def place(self, eid: str, floor: int) -> None:
        self.sim.cars[eid].current_floor = floor

    def preset_counters(self, eid: str, *, month: int, period: int, lifetime: int) -> None:
        c = self.sim.wear.cars[eid]
        c.floors_month, c.floors_period, c.floors_lifetime = month, period, lifetime
        c.starts_month, c.starts_period, c.starts_lifetime = month // 9, period // 9, lifetime // 9
        c.doors_month, c.doors_period, c.doors_lifetime = month // 8, period // 8, lifetime // 8

    def rider(self, eid: str, dest: int, label: str | None = None) -> int:
        """A passenger already inside car ``eid`` heading to ``dest``."""
        car = self.sim.cars[eid]
        p = self._new_person(car.current_floor, dest, label)
        p.state, p.car = "r", eid
        self.sim.submit_car_call(eid, dest)
        self._collect(minor_carcall=True)
        return p.pid

    def _new_person(self, origin: int, dest: int, label: str | None) -> Person:
        p = Person(self._next_pid, origin, dest, label, self.sim.sim_tick)
        self.people[p.pid] = p
        self._next_pid += 1
        return p

    # ---------------------------------------------------------------- narration
    def say(self, caption: str, pause: bool = True, on: str | None = None) -> None:
        """Caption for the next meaningful event (of type ``on`` when given): a key moment.

        With ``on``, an event of this tick that already happened (emitted by the action just
        before) is preferred, so a caption can describe the system's reaction to that action.
        """
        if on is not None:
            for ev in reversed(self.events):
                if ev["t"] != self.sim.sim_tick:
                    break
                if not ev["minor"] and not ev.get("caption") and (ev["type"] == on or on in ev.get("text", "")):
                    ev["caption"], ev["pause"] = caption, pause
                    return
        self._caption = (caption, pause, on)

    def criterion(self, text: str, ref: str, pred: Callable[["Story"], bool], kind: str = "eventually") -> None:
        self.criteria.append({"text": text, "ref": ref, "kind": kind, "pred": pred,
                              "passed_at": None, "failed_at": None})

    # ---------------------------------------------------------------- actions
    def _emit(self, ev: dict) -> dict:
        ev.setdefault("t", self.sim.sim_tick)
        ev.setdefault("minor", False)
        on = self._caption[2] if self._caption else None
        if self._caption and not ev["minor"] and (on is None or ev["type"] == on or on in ev.get("text", "")):
            ev["caption"], ev["pause"] = self._caption[0], self._caption[1]
            self._caption = None
        self.events.append(ev)
        return ev

    def act(self, text: str, fn: Callable[[OperationalSimulator], Any], *, lane: str = "stub",
            to: list[str] | None = None, code: str | None = None, car: str | None = None,
            who: str = "Test stub", why: dict | None = None, expect_error: bool = False) -> Any:
        """A person, staff member or equipment does something; the simulator reacts."""
        ev = self._emit({"type": "Action", "lane": lane, "to": to or [], "text": text,
                         "car": car, "who": who, "code": code, "why": why})
        try:
            result = fn(self.sim)
        except OperationalError as exc:
            if not expect_error:
                raise
            ev["result"] = {"ok": False, "text": str(exc)}
            result = exc
        else:
            if expect_error:
                ev["result"] = {"ok": True, "text": "accepted"}
        self._collect()
        self._resnap()
        return result

    def person(self, origin: int, dest: int, label: str | None = None, *, minor: bool | None = None) -> int:
        """Someone walks to the landing at ``origin`` and presses the hall button."""
        p = self._new_person(origin, dest, label)
        self._press(p, minor=self.traffic_minor if minor is None else minor)
        return p.pid

    def _direction(self, origin: int, dest: int) -> str:
        pos = lambda f: 0 if f == -1 else f
        return "UP" if pos(dest) > pos(origin) else "DOWN"

    def _press(self, p: Person, *, minor: bool, again: bool = False) -> None:
        if p.kind == "freight":
            self._press_freight(p, minor=minor, again=again)
            return
        direction = self._direction(p.origin, p.dest)
        before = {rid for rid in self.sim._active_hall}
        req, merged = self.sim.submit_hall_call(origin_floor=p.origin, direction=direction)
        p.req = req["request_id"]
        p.repress_at = None
        if p.label:
            self.labels[p.label] = p.req
        who = f"Passenger on floor {floor_name(p.origin)}"
        arrow = "↑" if direction == "UP" else "↓"
        if merged:
            self._emit({"type": "HallCallMerged", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"{floor_name(p.origin)}{arrow} pressed again: merged into the active request, no second assignment (FR-1.22).",
                        "code": "merge", "who": who,
                        "why": {"kind": "lifecycle", "machine": "Hall call", "states": HALL_STATES,
                                "current": req["status"]}})
        else:
            lead = "Car full — pressed again" if again else "Hall button pressed"
            self._emit({"type": "HallCall", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"{lead} on floor {floor_name(p.origin)} going {direction.lower()} (destination {floor_name(p.dest)}). No card needed (SEC-2.4).",
                        "code": "hall", "who": who, "request": p.req})
        self._collect(new_ids=set(self.sim._active_hall) - before, minor=minor)
        self._resnap()

    def _press_freight(self, p: Person, *, minor: bool, again: bool = False) -> None:
        """E5 freight key: no UP/DOWN; refused (not queued) while E5 is unavailable (FR-1.13)."""
        try:
            req, merged = self.sim.submit_hall_call(origin_floor=p.origin, call_type="FREIGHT")
        except OperationalError as exc:
            p.req, p.repress_at = None, self.sim.sim_tick + 5
            self._emit({"type": "HallCall", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"Freight key on floor {floor_name(p.origin)} refused: {exc}. They try again in 5 s.",
                        "code": "hall", "who": f"Resident with goods on floor {floor_name(p.origin)}",
                        "result": {"ok": False, "text": str(exc)}})
            return
        p.req, p.repress_at = req["request_id"], None
        if not merged:
            self._emit({"type": "HallCall", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"{'Freight key pressed again' if again else 'Freight key pressed'} on floor {floor_name(p.origin)} (goods to {floor_name(p.dest)}). Only E5 may answer (FR-1.4).",
                        "code": "hall", "who": f"Resident with goods on floor {floor_name(p.origin)}", "request": p.req})
        self._collect(new_ids={p.req}, minor=minor)
        self._resnap()

    def press(self, floor: int, direction: str, label: str | None = None, *, who: str = "Someone",
              minor: bool = False) -> str:
        """A hall-button press with nobody who will board (prank / impatient presses)."""
        req, merged = self.sim.submit_hall_call(origin_floor=floor, direction=direction)
        if label:
            self.labels[label] = req["request_id"]
        arrow = "↑" if direction == "UP" else "↓"
        if merged:
            self._emit({"type": "HallCallMerged", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"{who} presses {floor_name(floor)}{arrow} again: merged into {req['request_id'][:8]}, still one request (FR-1.22).",
                        "code": "merge", "who": who,
                        "why": {"kind": "lifecycle", "machine": "Hall call", "states": HALL_STATES,
                                "current": req["status"]}})
        else:
            self._emit({"type": "HallCall", "lane": "stub", "to": ["dispatch"], "minor": minor,
                        "text": f"{who} presses {direction.lower()} on floor {floor_name(floor)}.",
                        "code": "hall", "who": who, "request": req["request_id"]})
        self._collect(minor=minor)
        self._resnap()
        return req["request_id"]

    # ---------------------------------------------------------------- time
    def run(self, seconds: int) -> None:
        for _ in range(seconds):
            self._tick()

    def run_until(self, pred: Callable[["Story"], bool], limit: int = 600) -> None:
        for _ in range(limit):
            if pred(self):
                return
            self._tick()
        raise RuntimeError(f"{self.meta['key']}: condition not reached within {limit} s")

    def _tick(self) -> None:
        if not self.started:
            self.snap()
            self.started = True
        self.sim.advance(1)
        self._collect()          # arrivals and assignments first, then who walks in or out
        self._passengers()
        self._report_loads()
        self._collect()
        self._door_tracking()
        self._evaluate()
        self.snap()

    def _door_tracking(self) -> None:
        for eid, car in self.sim.cars.items():
            if car.door_state == "OPEN" and self._last_door[eid] != "OPEN":
                self.door_opens[eid].append((self.sim.sim_tick, car.current_floor))
            self._last_door[eid] = car.door_state

    # ---------------------------------------------------------------- passengers
    def _report_loads(self) -> None:
        """The test-stub load sensor: people on board / rated capacity, when it changes."""
        for eid, car in self.sim.cars.items():
            frac = len(self.riders(eid)) / CAPACITY[eid]
            if car.load_fraction != frac:
                self.sim.report_load(eid, frac)

    def riders(self, eid: str) -> list[Person]:
        return [p for p in self.people.values() if p.state == "r" and p.car == eid]

    def _passengers(self) -> None:
        t = self.sim.sim_tick
        for p in list(self.people.values()):
            if p.state == "x":
                p.exit_ttl -= 1
                if p.exit_ttl <= 0:
                    del self.people[p.pid]
        # Alight first, then board.
        for p in list(self.people.values()):
            if p.state != "r" or p.car is None:
                continue
            car = self.sim.cars[p.car]
            responder = car.availability == "EMERGENCY_UNAVAILABLE" and car.current_floor == 1 \
                and car.door_state == "OPEN" and not car.medical_run
            if car.door_state == "OPEN" and (car.current_floor == p.dest or responder):
                self.journeys.append({"pid": p.pid, "kind": p.kind, "from": p.start, "to": p.dest,
                                      "at": car.current_floor, "car": p.car, "appear": p.since,
                                      "board": p.t_board, "arrive": t})
                p.state, p.exit_ttl, p.origin = "x", 2, car.current_floor
                self._emit({"type": "PassengerExit", "lane": "stub", "to": [], "minor": True,
                            "car": p.car,
                            "text": f"Passenger leaves {p.car} at floor {floor_name(car.current_floor)}"
                                    + (" (responder floor)." if responder and car.current_floor != p.dest else ".")})
        for p in list(self.people.values()):
            if p.state != "w":
                continue
            if p.repress_at is not None:
                if t >= p.repress_at:
                    self._press(p, minor=True, again=True)
                continue
            req = self.sim.hall_calls.get(p.req) if p.req else None
            if req is None or req.status in ("CANCELLED", "FAILED"):
                p.repress_at = t + 3
                continue
            if req.status != "COMPLETED" or req.assigned_car is None:
                continue
            car = self.sim.cars[req.assigned_car]
            if car.current_floor != p.origin or car.door_state != "OPEN":
                if req.status == "COMPLETED":
                    p.repress_at = t + 2  # the car left without them
                continue
            if len(self.riders(car.elevator_id)) >= CAPACITY[car.elevator_id]:
                p.repress_at = t + 2
                continue
            p.state, p.car, p.t_board = "r", car.elevator_id, t
            self._emit({"type": "PassengerBoard", "lane": "stub", "to": [], "minor": True,
                        "car": car.elevator_id,
                        "text": f"Passenger boards {car.elevator_id} at floor {floor_name(p.origin)}."})
            if p.dest != car.current_floor and car.availability in ("IN_SERVICE", "OCCUPIED"):
                try:
                    self.sim.submit_car_call(car.elevator_id, p.dest)
                except OperationalError:
                    pass
                self._collect(minor_carcall=self.traffic_minor)

    # ---------------------------------------------------------------- collect events
    def _collect(self, new_ids: set | None = None, minor: bool = False, minor_carcall: bool = False) -> None:
        sim = self.sim
        for d in sim.decision_trace[self._seen_decisions:]:
            self._decisions[d["request_id"]] = d
        self._seen_decisions = len(sim.decision_trace)

        for e in sim.events[self._seen_events:]:
            self._map_event(e, minor=minor, minor_carcall=minor_carcall)
        self._seen_events = len(sim.events)

        for cmd in sim.commands[self._seen_cmds:]:
            if cmd["status"] in ("REJECTED", "TIMED_OUT", "UNKNOWN"):
                self._emit({"type": "Command", "lane": "stub", "to": ["audit"], "car": cmd["elevator_id"],
                            "minor": minor and cmd["status"] == "REJECTED",
                            "text": f"Interface command {cmd['command_type']} → {cmd['status']}: {cmd['detail']}",
                            "code": "command",
                            "why": {"kind": "command", "type": cmd["command_type"], "status": cmd["status"],
                                    "detail": cmd["detail"]}})
        self._seen_cmds = len(sim.commands)

        for eid, car in sim.cars.items():
            if car.availability != self._avail[eid]:
                before, after = self._avail[eid], car.availability
                self._avail[eid] = after
                lane = "emergency" if after == "EMERGENCY_UNAVAILABLE" or before == "EMERGENCY_UNAVAILABLE" else "maintenance"
                if after == "OCCUPIED" or before == "OCCUPIED":
                    lane = "dispatch"
                code = {"STALE": "comms", "MAINTENANCE": "isolate", "FAULT": "fault",
                        "IN_SERVICE": "return"}.get(after, "isolate")
                self._emit({"type": "Availability", "lane": lane, "to": ["dispatch", "audit"], "car": eid,
                            "text": f"{eid}: {before.replace('_', ' ').lower()} → {after.replace('_', ' ').lower()}."
                                    + (" Excluded from new assignments (FR-1.6)." if after not in ("IN_SERVICE",) else " Eligible for dispatch again."),
                            "code": code,
                            "why": {"kind": "lifecycle", "machine": f"{eid} availability",
                                    "states": ["IN_SERVICE", "OCCUPIED", "MAINTENANCE", "FAULT", "EMERGENCY_UNAVAILABLE", "STALE"],
                                    "current": after, "previous": before}})

        for rid, req in sim.hall_calls.items():
            prev = self._reassign.get(rid, 0)
            if req.reassignment_count > prev:
                self._reassign[rid] = req.reassignment_count
                self._emit({"type": "Requeued", "lane": "dispatch", "to": [], "minor": minor,
                            "text": f"Hall call {floor_name(req.origin_floor)}{'↑' if req.direction == 'UP' else '↓' if req.direction == 'DOWN' else ''} back in the queue; original acceptance time kept for the wait-time record (FR-1.7, FR-1.18).",
                            "why": {"kind": "lifecycle", "machine": "Hall call", "states": HALL_STATES,
                                    "current": req.status, "note": "ASSIGNED/APPROACHING → QUEUED on reassignment"}})

    def _map_event(self, e: dict, *, minor: bool, minor_carcall: bool) -> None:
        sim = self.sim
        kind, eid, detail = e["event_type"], e["elevator_id"], e["detail"]
        ev: dict | None = None
        if kind == "ASSIGNMENT":
            rid = detail.split()[2]
            d = self._decisions.get(rid)
            req = sim.hall_calls.get(rid)
            arrow = "" if req is None or req.direction is None else ("↑" if req.direction == "UP" else "↓")
            text = f"Dispatch assigns {eid} to {floor_name(req.origin_floor if req else None)}{arrow}"
            if d:
                row = next(c for c in d["candidates"] if c["car"] == eid)
                fastest = min(d["candidates"], key=lambda c: c["eta"])
                text += f": predicted {row['eta']} s"
                if row["penalty"] >= 0.05:
                    text += f" + wear {row['penalty']:.1f} s"
                if fastest["car"] != eid and fastest["eta"] < row["eta"]:
                    text += f" (faster {fastest['car']} has a higher cost)"
                text += "."
            ev = {"type": "Assignment", "lane": "dispatch", "to": ["stub"], "car": eid, "text": text,
                  "code": "assign", "why": {"kind": "decision", **d} if d else None,
                  "minor": minor}
        elif kind == "LOAD_FULL":
            ev = {"type": "LoadFull", "lane": "stub", "to": ["dispatch"], "car": eid, "minor": minor,
                  "text": f"{eid} is full ({CAPACITY[eid]} people). Load sensor reports it; Dispatch stops giving it hall calls (FR-1.14).",
                  "code": "load"}
        elif kind == "FULL_LOAD_BYPASS":
            ev = {"type": "Bypass", "lane": "dispatch", "to": ["stub"], "car": eid, "minor": False,
                  "text": f"{eid} passes its pickups because it is full: {detail.split(': ', 1)[-1]} to other cars (full-load bypass, FR-1.14).",
                  "code": "bypass"}
        elif kind == "HALL_ARRIVAL":
            self.served_floors[eid].append(sim.cars[eid].current_floor)
            ev = {"type": "Arrival", "lane": "stub", "to": ["dispatch"], "car": eid,
                  "text": f"{eid} arrives at floor {floor_name(sim.cars[eid].current_floor)}, doors open. Waited {detail.split()[1].rstrip('s')} s (FR-1.18).",
                  "code": "arrive", "minor": minor or self.traffic_minor,
                  "why": {"kind": "lifecycle", "machine": "Hall call", "states": HALL_STATES, "current": "COMPLETED"}}
        elif kind == "CAR_CALL":
            car = sim.cars[eid]
            dest = int(detail.split()[-1])
            floors = [s.floor for s in car.stops]
            idx = max((i for i, s in enumerate(car.stops) if s.kind == "CAR" and s.floor == dest), default=len(floors) - 1)
            on_way = idx < len(floors) - 1
            route = " → ".join(floor_name(f) for f in floors)
            ev = {"type": "CarCall", "lane": "dispatch", "to": ["stub"], "car": eid,
                  "text": f"Destination {floor_name(dest)} queued in {eid}" + (" — served on the way" if on_way else "") + f". Route: {route}.",
                  "code": "carcall", "minor": minor_carcall,
                  "why": {"kind": "route", "car": eid, "stops": [[s.floor, KIND_CODE[s.kind]] for s in car.stops],
                          "highlight": idx, "from": car.current_floor}}
        elif kind == "INCIDENT_OPEN":
            inc_kind = detail.split(":")[0]
            inc = sim.incidents[detail.split(": ")[1]]
            labels = {"HELP": "HELP request", "MEDICAL": "Medical emergency", "STUCK_CAR": "Stuck car",
                      "DOOR_OBSTRUCTION": "Door obstruction > 10 s", "COMMUNICATION_LOSS": "Communication loss",
                      "BLOCKING_FAULT": "Blocking fault", "FIRE": "Fire alarm", "POWER_FAILURE": "Power failure",
                      "REPEATED_EMERGENCY": "Repeated emergency triggers", "REPEATED_EMPTY_CALLS": "Repeated empty calls"}
            where = f" at floor {floor_name(inc.floor)}" if inc.floor is not None else ""
            ev = {"type": "IncidentOpened", "lane": "emergency", "to": ["audit"], "car": eid,
                  "text": f"Incident opened: {labels.get(inc_kind, inc_kind)}{' in ' + eid if eid else ''}{where}, severity {inc.severity.lower()}. Security notified (UI-7).",
                  "code": "incident",
                  "why": {"kind": "lifecycle", "machine": "Incident", "states": INCIDENT_STATES, "current": "OPEN",
                          "fields": {"type": inc.kind, "scope": inc.scope, "severity": inc.severity,
                                     "floor": floor_name(inc.floor), "direction": inc.direction or "—",
                                     "trigger": inc.trigger_source, "related requests": len(inc.related_requests)}}}
        elif kind == "INCIDENT_ESCALATION":
            inc = sim.incidents[detail.split(": ")[1]]
            ev = {"type": "Escalation", "lane": "emergency", "to": ["audit"], "car": eid,
                  "text": f"{inc.kind.replace('_', ' ').title()} still open after 60 s: security notified again (EMR-3.9).",
                  "code": "escalate",
                  "why": {"kind": "lifecycle", "machine": "Incident", "states": INCIDENT_STATES, "current": inc.status,
                          "note": "escalated"}}
        elif kind.startswith("INCIDENT_"):
            action = kind.split("_", 1)[1]
            inc = sim.incidents[detail.split()[0]]
            ev = {"type": "IncidentAction", "lane": "emergency", "to": ["audit"], "car": eid,
                  "text": f"{inc.kind.replace('_', ' ').title()}: {action.replace('_', ' ').lower()} by {detail.split(' by ')[-1]}.",
                  "code": "resolve",
                  "why": {"kind": "lifecycle", "machine": "Incident", "states": INCIDENT_STATES + ["FALSE_ALARM"],
                          "current": inc.status}}
        elif kind == "MEDICAL_RESPONDER_RUN":
            ev = {"type": "ResponderRun", "lane": "emergency", "to": ["stub"], "car": eid,
                  "text": f"{eid} runs non-stop to the responder floor (floor 1); its other stops are dropped (EMR-3.14).",
                  "code": "medical"}
        elif kind == "MEDICAL_ARRIVAL":
            ev = {"type": "ResponderArrival", "lane": "stub", "to": ["emergency"], "car": eid,
                  "text": f"{eid} reached floor 1, doors open for the responders.", "code": "arrive"}
        elif kind == "RESCUE_DISPATCH":
            ev = {"type": "RescueDispatch", "lane": "emergency", "to": ["stub", "dispatch"], "car": eid,
                  "text": f"Rescue car {eid} sent to the patient's floor, doors to be held {detail.split('wait ')[-1].rstrip('s')} s. Its pending hall calls are requeued (EMR-3.12).", "code": "rescue"}
        elif kind == "RESCUE_ARRIVAL":
            ev = {"type": "RescueArrival", "lane": "stub", "to": ["emergency"], "car": eid,
                  "text": f"{eid} at the patient's floor, doors held open (this wait is not a door fault).", "code": "arrive"}
        elif kind == "RESCUE_BOARDING":
            ev = {"type": "RescueBoarding", "lane": "emergency", "to": ["stub"], "car": eid,
                  "text": f"Admin confirms the patient is aboard {eid}: non-stop run to floor 1.", "code": "rescue_board"}
        elif kind in ("RESCUE_RELEASE", "RESCUE_CANCELLED", "RESCUE_EXPIRED"):
            ev = {"type": "RescueRelease", "lane": "emergency", "to": ["dispatch"], "car": eid,
                  "text": f"{eid} released from rescue duty ({detail}).", "code": "rescue"}
        elif kind in ("OBSTRUCTION", "OBSTRUCTION_CLEARED"):
            active = kind == "OBSTRUCTION"
            ev = {"type": "Obstruction", "lane": "stub", "to": ["emergency"], "car": eid,
                  "text": f"Door sensor on {eid}: " + ("obstruction ACTIVE — door state OBSTRUCTED, close inhibited (FR-1.15)." if active else "obstruction cleared — doors may close."),
                  "code": "obstruction"}
        elif kind == "DOOR_COMMAND":
            ev = {"type": "DoorCommand", "lane": "stub", "to": [], "car": eid, "text": f"Door {detail.lower()} accepted.",
                  "code": "door", "minor": True}
        elif kind == "RECONCILED":
            ev = {"type": "Reconciled", "lane": "maintenance", "to": ["dispatch", "audit"], "car": eid,
                  "text": f"{eid} reconnected: fresh snapshot taken, unknown outcomes marked FAILED, nothing replayed (MNT-4.9, COM-4).",
                  "code": "reconcile"}
        elif kind == "FAULT_CLEARED":
            ev = {"type": "FaultCleared", "lane": "stub", "to": ["maintenance"], "car": eid,
                  "text": f"Equipment reports fault {detail} cleared on {eid}. The car stays isolated until someone returns it.",
                  "code": "fault"}
        elif kind == "HALL_CANCELLED":
            ev = {"type": "Cancelled", "lane": "dispatch", "to": ["audit"], "text": f"Hall call cancelled: {detail.split(' by ')[1]} (FR-1.20).",
                  "code": "cancel"}
        elif kind == "NO_BOARDING":
            ev = {"type": "NoBoarding", "lane": "audit", "to": [], "car": eid,
                  "text": "Nobody boarded within 30 s: the call stays COMPLETED and is flagged NO_BOARDING (FR-1.21).",
                  "code": "noboard"}
        elif kind == "BOARDING_EVIDENCE":
            ev = {"type": "BoardingEvidence", "lane": "stub", "to": ["dispatch"], "car": eid, "minor": True,
                  "text": "Boarding evidence: " + ("passenger boarded." if ("boarded=True" in detail or detail.startswith("Car call")) else "nobody boarded (labelled test stub)."),
                  "code": "evidence"}
        elif kind in ("WEAR_WINDOW_CLOSED", "WEAR_LIMIT_EXCEEDED", "TRIP_COUNTER_RESET"):
            ev = {"type": "WearWindow", "lane": "audit", "to": ["dispatch"],
                  "text": detail + ("" if kind != "TRIP_COUNTER_RESET" else "."), "code": "wear",
                  "why": {"kind": "wear", "rows": [r for r in sim.wear.month_history[-2:]]},
                  # E5 is a wear group of its own (spread always 0 %): routine, not a key step
                  "minor": "FREIGHT" in detail}
        elif kind == "BACKLOG_EXCEEDED":
            ev = {"type": "Backlog", "lane": "audit", "to": [], "text": detail}
        if ev is None:
            return
        ev.setdefault("minor", False)
        ev["t"] = sim.sim_tick
        self._emit(ev)

    # ---------------------------------------------------------------- criteria
    def _evaluate(self) -> None:
        t = self.sim.sim_tick
        for c in self.criteria:
            if c["kind"] == "eventually" and c["passed_at"] is None and c["pred"](self):
                c["passed_at"] = t
            elif c["kind"] == "always" and c["failed_at"] is None and not c["pred"](self):
                c["failed_at"] = t

    def finish(self, strict: bool = True) -> dict:
        self._caption = None
        end = self.sim.sim_tick
        for c in self.criteria:
            if c["kind"] == "always" and c["failed_at"] is None:
                c["passed_at"] = end
            elif c["kind"] == "final":
                if c["pred"](self):
                    c["passed_at"] = end
                else:
                    c["failed_at"] = end
        crit = [{k: v for k, v in c.items() if k != "pred"} for c in self.criteria]
        failed = [c for c in crit if c["passed_at"] is None]
        if failed and strict:
            raise AssertionError(f"{self.meta['key']}: criteria not met: {[c['text'] for c in failed]}")
        for c in self.criteria:
            c.pop("pred", None)
        for i, e in enumerate(self.events):
            e["i"] = i
        return {**self.meta, "until": end, "frames": self.frames, "events": self.events,
                "criteria": crit, "strings": self.strings,
                "counters_start": self.flags.get("counters_start")}

    # ---------------------------------------------------------------- frames
    def _string(self, s: str) -> int:
        if s not in self.strings:
            self.strings.append(s)
        return self.strings.index(s)

    def _resnap(self) -> None:
        """Actions at tick T (a press, a command) change state after frame T was taken; retake it."""
        if self.started and self.frames and self.frames[-1]["t"] == self.sim.sim_tick:
            self.frames.pop()
            self.snap()

    def snap(self) -> None:
        sim = self.sim
        cars = []
        for eid, c in sim.cars.items():
            flags = ""
            if c.medical_run:
                flags += "m"
            if c.rescue_active:
                flags += "r"
            if c.active_fault:
                flags += "f"
            if not c.connected:
                flags += "s"
            door = {"OPEN": "O", "CLOSED": "C", "OBSTRUCTED": "X"}.get(c.door_state, "C")
            cars.append([c.current_floor, c.direction[0], door, AVAIL_CODE[c.availability],
                         [[s.floor, KIND_CODE[s.kind]] for s in c.stops], flags,
                         sim.wear.cars[eid].floors_month, len(self.riders(eid)),
                         self._string(c.message)])
        calls = []
        for rid in sim._active_hall:
            r = sim.hall_calls[rid]
            if r.status in TERMINAL_STATUSES:
                continue
            d = "F" if r.call_type == "FREIGHT" else r.direction[0]
            calls.append([r.origin_floor, d, r.status[0], r.assigned_car])
        people = [[p.pid, p.state, p.origin if p.state != "r" else None, p.car, p.dest]
                  for p in self.people.values()]
        incidents = [[i.kind, i.elevator_id, i.severity[0], 1 if i.escalated else 0, i.status[0]]
                     for i in sim.incidents.values() if i.status not in ("RESOLVED", "CLOSED", "FALSE_ALARM")]
        waits = sorted(r.wait_seconds for r in sim.hall_calls.values()
                       if r.wait_seconds is not None and r.call_type == "ORDINARY")
        mean = round(sum(waits) / len(waits), 1) if waits else None
        p95 = waits[max(0, math.ceil(0.95 * len(waits)) - 1)] if waits else None
        active = len([1 for rid in sim._active_hall if sim.hall_calls[rid].status not in TERMINAL_STATUSES]) \
            + len([1 for rid in sim._active_car if sim.car_calls[rid].status not in ("COMPLETED", "REJECTED", "CANCELLED", "FAILED")])
        self.frames.append({"t": sim.sim_tick, "c": cars, "h": calls, "p": people, "i": incidents,
                            "m": [len(waits), mean, p95, active]})


# =========================================================================== stories
def _decision(s: Story, label: str) -> dict | None:
    rid = s.labels.get(label)
    return s._decisions.get(rid) if rid else None


def _row(d: dict | None, car: str) -> dict | None:
    if not d:
        return None
    return next((c for c in d["candidates"] if c["car"] == car), None)


def story_dispatch() -> dict:
    s = Story("dispatch", "Direction-aware dispatch", vt=["VT-01", "VT-05"], mode="Mode 1 · Dispatch",
              tagline="E1 is the closest car, but it is heading down to the lobby. Dispatch compares predicted service time, not distance.",
              requirements=["FR-1.3", "FR-1.4", "FR-1.5", "FR-1.18", "FR-1.24", "SEC-2.4"],
              base=datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc))
    s.place("E1", 12); s.place("E2", 5); s.place("E3", 1); s.place("E4", 23); s.place("E5", 15)
    s.rider("E1", 1)
    s.criterion("Hall call accepted without a card and recorded with floor, direction, ID and time", "FR-1.3",
                lambda s: "f10" in s.labels and s.sim.hall_calls[s.labels["f10"]].status != "RECEIVED")
    s.criterion("Nearest car E1 (moving away) is not chosen; E2 wins on predicted time", "FR-1.4",
                lambda s: (d := _decision(s, "f10")) is not None and d["chosen"] == "E2"
                and _row(d, "E1")["eta"] > _row(d, "E2")["eta"])
    s.criterion("Floor 7 is picked up on E2's way up, before floor 10", "FR-1.4",
                lambda s: (d := _decision(s, "f7")) is not None and d["chosen"] == "E2" and _row(d, "E2")["index"] == 0)
    s.criterion("Wait time recorded from acceptance to arrival", "FR-1.18",
                lambda s: "f10" in s.labels and s.sim.hall_calls[s.labels["f10"]].wait_seconds is not None)
    s.criterion("E2's destinations served in travel order (15, 20, 24) with no zig-zag", "FR-1.24",
                lambda s: [f for _, f in s.door_opens["E2"] if f in (15, 20, 24)] == [15, 20, 24], kind="final")
    s.run(1)
    s.say("A resident on floor 10 wants to go up to 24. E1 is only two floors away, but it is carrying someone down to the lobby. Watch which car Dispatch picks.")
    s.person(10, 24, "f10")
    s.person(10, 15, None)
    s.say("Floor 7 lies on E2's way up from 5 to 10, in the same direction. It is added in front of floor 10: no detour, only a door stop.",
          on="to 7↑")
    s.person(7, 20, "f7")
    s.run(3)
    s.person(15, 30, "f15", minor=True)
    s.say("Two people board at floor 10: one for 24, one for 15. Floor 15 lies between 10 and 20, so it is inserted where the car passes it, not at the end.",
          on="served on the way")
    s.run(4)
    s.run_until(lambda s: not any(p.state != "x" for p in s.people.values()) and all(not c.stops for c in s.sim.cars.values()), 120)
    s.say("All destinations served in travel order. The table shows every wait recorded from acceptance to arrival.", pause=False)
    s.run(4)
    return s.finish()


def story_wear() -> dict:
    base = datetime(2026, 11, 30, 23, 58, 0, tzinfo=timezone.utc)
    s = Story("wear", "Wear-and-tear balancing and month close", vt=["VT-33", "VT-36"], mode="Instructor NFR · Wear",
              tagline="E2 was in maintenance for two days and is 9,050 floors behind this month. Dispatch lets it catch up — but never makes anyone wait more than 15 s extra.",
              requirements=["WEAR-1", "WEAR-2", "WEAR-3", "WEAR-4", "FR-1.5", "PERF-9"],
              base=base, period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
              note="Trip counters start from realistic month-to-date values (preset); every floor after that is counted by the simulator.")
    month = {"E1": 618_420, "E2": 609_370, "E3": 618_210, "E4": 618_330, "E5": 101_240}
    for eid, m in month.items():
        s.preset_counters(eid, month=m, period=m + 1_802_000, lifetime=m + 9_640_000)
    s.flags["counters_start"] = month
    s.place("E1", 4); s.place("E2", 1); s.place("E3", 12); s.place("E4", 30); s.place("E5", 8)
    s.criterion("Least-used eligible car wins when it is reasonably close (E2 over the faster E1)", "WEAR-3",
                lambda s: (d := _decision(s, "w6")) is not None and d["chosen"] == "E2" and _row(d, "E1")["eta"] < _row(d, "E2")["eta"])
    s.criterion("Penalty never exceeds the 15 s cap", "WEAR-3",
                lambda s: all(c["penalty"] <= d["cap"] + 1e-9 for d in s.sim.decision_trace for c in d["candidates"]),
                kind="always")
    s.criterion("A much faster car still wins: the cap protects waiting time", "PERF-9",
                lambda s: (d := _decision(s, "w3")) is not None and d["chosen"] == "E1")
    s.criterion("Every floor travelled is added to the trip counter", "WEAR-1",
                lambda s: s.sim.wear.cars["E2"].floors_lifetime > 609_370 + 9_640_000)
    s.criterion("November closes at midnight with spread < 2 %; month counters reset", "WEAR-2, WEAR-4",
                lambda s: any(r["wear_group"] == "RESIDENTIAL" and r["within_limit"] for r in s.sim.wear.month_history)
                and s.sim.wear.cars["E1"].floors_month < 100)
    s.criterion("6-month trip counter keeps counting across the month boundary", "WEAR-5",
                lambda s: s.sim.wear.month_history and s.sim.wear.cars["E1"].floors_period > 1_802_000)
    s.run(1)
    s.say("Floor 6 calls up. E1 is 2 s away, E2 5 s. But E1 has travelled 9,050 floors more this month: its cost carries the full 15 s penalty. E2 wins and starts to catch up.")
    s.person(6, 25, "w6")
    s.run(9)
    s.say("Now floor 3 calls up. E2 is busy going to 25 and would need about 40 s. E1 is 1 s away: even with the 15 s penalty it is faster, so E1 wins. Balancing never costs more than 15 s.")
    s.person(3, 20, "w3")
    s.run(30)
    s.person(16, 1, "w16", minor=True)
    s.run(25)
    s.person(9, -1, "w9", minor=True)
    s.say("Midnight: the simulator closes November. It compares the floors each car travelled this month against the 2 % limit and resets the month counters. E2 lost two days and still ends inside the limit.",
          on="WearWindow")
    s.run_until(lambda s: len(s.sim.wear.month_history) > 0, 200)
    s.run(20)
    return s.finish()


def story_peak() -> dict:
    s = Story("peak", "Morning down-peak, 4 minutes", vt=["VT-23", "VT-24"], mode="Performance",
              tagline="07:40 on a Monday: residents leave for work. Complete journeys — hall call, boarding, destination — against the PERF-9 targets.",
              requirements=["PERF-9", "PERF-5", "FR-1.22", "FR-1.23", "CAP-2"],
              base=datetime(2026, 11, 23, 7, 40, tzinfo=timezone.utc), traffic_minor=True)
    s.place("E1", 1); s.place("E2", 9); s.place("E3", 17); s.place("E4", 25); s.place("E5", 12)
    rng = random.Random(7)
    s.criterion("Average wait ≤ 60 s", "PERF-9",
                lambda s: s.frames and s.frames[-1]["m"][0] >= 30 and s.frames[-1]["m"][1] <= 60, kind="final")
    s.criterion("95th-percentile wait ≤ 120 s", "PERF-9",
                lambda s: s.frames and s.frames[-1]["m"][2] is not None and s.frames[-1]["m"][2] <= 120, kind="final")
    s.criterion("Active requests stay bounded (≤ 100)", "PERF-5",
                lambda s: s.frames[-1]["m"][3] <= 100 if s.frames else True, kind="always")
    s.criterion("E5 never takes an ordinary hall call", "FR-1.4",
                lambda s: all(d["chosen"] != "E5" for d in s.sim.decision_trace if d["call_type"] == "ORDINARY"), kind="always")
    s.criterion("Every passenger reaches a destination; no request dropped", "PERF-5",
                lambda s: all(p.state == "x" for p in s.people.values()), kind="final")
    s.say("Each figure is a resident with a destination. 70 % head down to the lobby or B1. Watch the cars collect people on the way down.")
    s.run(1)
    t_end = 240
    next_t = 0.0
    while s.sim.sim_tick < t_end:
        while next_t <= s.sim.sim_tick:
            roll = rng.random()
            lobby = -1 if rng.random() < 0.25 else 1
            if roll < 0.70:
                s.person(rng.randint(2, 30), lobby)
            elif roll < 0.90:
                s.person(lobby, rng.randint(2, 30))
            else:
                a, b = rng.sample(range(2, 31), 2)
                s.person(a, b)
            next_t += rng.expovariate(0.22)
        s.run(1)
        if s.sim.sim_tick == 120:
            s.say("Two minutes in. The readings panel shows the live average and 95th-percentile wait against the 60 s / 120 s targets.", pause=False)
            s._emit({"type": "Readings", "lane": "audit", "to": [], "text": f"{s.frames[-1]['m'][0]} hall calls served so far; mean wait {s.frames[-1]['m'][1]} s."})
    s.run_until(lambda s: all(p.state == "x" for p in s.people.values()), 300)
    s.say("Everyone delivered. Final readings are below — compare them with PERF-9.", pause=False)
    s._emit({"type": "Readings", "lane": "audit", "to": [], "text": f"Done: {s.frames[-1]['m'][0]} hall calls, mean wait {s.frames[-1]['m'][1]} s, 95th percentile {s.frames[-1]['m'][2]} s."})
    s.run(3)
    return s.finish()


def story_calls() -> dict:
    s = Story("calls", "Repeated presses, a cancel and empty calls", vt=["VT-15", "VT-16"], mode="Mode 1 · Dispatch",
              tagline="Five presses make one request. Only an Admin can cancel. Five empty calls from one floor raise a low-severity alert — and calls are still served.",
              requirements=["FR-1.20", "FR-1.21", "FR-1.22", "SEC-2.4"],
              base=datetime(2026, 11, 23, 14, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 14); s.place("E3", 28); s.place("E4", 30); s.place("E5", 3)
    s.criterion("Five presses of the same key leave one request", "FR-1.22",
                lambda s: "f9" in s.labels and len([r for r in s.sim.hall_calls.values() if r.origin_floor == 9]) == 1
                and len([e for e in s.events if e["type"] == "HallCallMerged"]) >= 4)
    s.criterion("Admin cancellation recorded with actor and reason", "FR-1.20",
                lambda s: "f21" in s.labels and s.sim.hall_calls[s.labels["f21"]].status == "CANCELLED"
                and s.sim.hall_calls[s.labels["f21"]].cancelled_by == "ADMIN-CARD-0001")
    s.criterion("Empty call stays COMPLETED, flagged NO_BOARDING, wait time recorded", "FR-1.21",
                lambda s: any(r.no_boarding and r.status == "COMPLETED" and r.wait_seconds is not None for r in s.sim.hall_calls.values()))
    s.criterion("Five NO_BOARDING calls from floor 4 within 10 min raise one low-severity alert", "FR-1.22",
                lambda s: any(i.kind == "REPEATED_EMPTY_CALLS" and i.severity == "LOW" for i in s.sim.incidents.values()))
    s.criterion("Calls are still served during the alert", "FR-1.22",
                lambda s: "after" in s.labels and s.sim.hall_calls[s.labels["after"]].status == "COMPLETED")
    s.run(1)
    s.say("Someone on floor 9 is in a hurry and presses DOWN five times. Dispatch must keep exactly one request and one assignment.")
    s.person(9, 1, "f9")
    for _ in range(4):
        s.run(1)
        s.press(9, "DOWN", who="The same passenger")
    s.run(2)
    s.say("A prank call on floor 21. A passenger cannot cancel it (no cancel key on the hall panel). The Admin cancels it from the dashboard; the actor and reason are recorded.")
    rid = s.press(21, "UP", "f21", who="Someone")
    s.run(2)
    s.act("Admin ADMIN-CARD-0001 cancels the floor 21 call from the dashboard (reason: prank call).",
          lambda sim: sim.cancel_hall_call(rid, "ADMIN-CARD-0001", reason="Prank call reported by security"),
          lane="dispatch", to=["audit"], who="Admin", code="cancel")
    for n in range(5):
        if n == 0:
            s.say("Floor 4: someone presses UP and walks away. The car arrives and nobody boards. The test stub reports 'no boarding' (SWI-2); after 30 s the call is flagged.")
        rid = s.press(4, "UP", f"e{n}", who="Someone", minor=n > 0)
        s.run_until(lambda s, rid=rid: s.sim.hall_calls[rid].status == "COMPLETED", 120)
        s.act("Test stub: no load change, no car call — nobody boarded.", lambda sim, rid=rid: sim.boarding_evidence(rid, False),
              code="evidence", who="Test stub")
        if n == 4:
            s.say("The fifth empty call from floor 4 within 10 minutes: a low-severity security alert links them (FR-1.22).")
        s.run(32)
        s.run(6)
    s.say("Calls are never blocked by the alert: a real passenger on floor 4 is still served.")
    s.person(4, 18, "after")
    s.run_until(lambda s: s.sim.hall_calls[s.labels["after"]].status == "COMPLETED", 120)
    s.run(8)
    return s.finish()


def story_help() -> dict:
    s = Story("help", "HELP request, then a stuck car", vt=["VT-30", "VT-21"], mode="Mode 3 · Emergency",
              tagline="HELP alone does not stop the car. Repeated presses raise a low alert. Only when the Admin confirms it stuck does E3 leave dispatch.",
              requirements=["EMR-3.1", "EMR-3.3", "EMR-3.15", "FR-1.18", "MNT-4.6"],
              base=datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 1); s.place("E3", 9); s.place("E4", -1); s.place("E5", 6)
    s.rider("E3", 22)
    state: dict = {}
    s.criterion("HELP creates an incident and an in-car message; E3 stays in service", "EMR-3.1",
                lambda s: any(i.kind in ("HELP", "STUCK_CAR") for i in s.sim.incidents.values())
                and state.get("eligible_after_help") is True)
    s.criterion("Four triggers within 60 s add one low-severity alert", "EMR-3.15",
                lambda s: len([i for i in s.sim.incidents.values() if i.kind == "REPEATED_EMERGENCY"]) == 1)
    s.criterion("Confirmed stuck: E3 EMERGENCY_UNAVAILABLE, its hall call requeued to another car", "EMR-3.3",
                lambda s: "f20" in s.labels and s.sim.hall_calls[s.labels["f20"]].reassignment_count >= 1
                and s.sim.hall_calls[s.labels["f20"]].assigned_car not in (None, "E3"))
    s.criterion("The requeued call keeps its original acceptance time", "FR-1.18",
                lambda s: "f20" in s.labels and s.sim.hall_calls[s.labels["f20"]].wait_seconds is not None
                and s.sim.hall_calls[s.labels["f20"]].accepted_at == s.sim.base_time + timedelta(seconds=1))
    s.criterion("Car call blocked, not falsely completed", "EMR-3.3",
                lambda s: any(c.elevator_id == "E3" and c.status.startswith("BLOCKED") for c in s.sim.car_calls.values()))
    s.criterion("Back in service only after recovery checks and confirmation", "MNT-4.6",
                lambda s: s.sim.cars["E3"].availability == "IN_SERVICE" and any(
                    i.kind == "STUCK_CAR" and i.status == "RESOLVED" for i in s.sim.incidents.values()))
    s.run(1)
    s.person(20, 27, "f20")
    s.run(2)
    s.say("The passenger in E3 presses HELP. Security is notified and the car shows a message — but HELP alone does not take E3 out of service.")
    inc = s.act("Passenger in E3 presses HELP (no card needed).", lambda sim: sim.trigger_emergency("E3", "HELP"),
                car="E3", who="Passenger in E3", code="help")
    state["eligible_after_help"] = s.sim._eligible(s.sim.cars["E3"])
    for k in range(3):
        s.run(1)
        if k == 2:
            s.say("Fourth press within 60 s: every trigger is recorded as valid, and one low-severity alert links them (EMR-3.15).")
        s.act("HELP pressed again.", lambda sim: sim.trigger_emergency("E3", "HELP"), car="E3", who="Passenger in E3", code="help")
    s.run(1)
    s.say("The Admin calls the intercom: the car has stopped responding. Confirming the HELP request turns it into a stuck-car emergency. E3's hall call goes to another car.")
    s.act("Admin confirms the HELP request as a stuck car.", lambda sim: sim.confirm_help_as_stuck(inc["incident_id"], actor="ADMIN-CARD-0001"),
          lane="emergency", to=["dispatch", "audit"], who="Admin", car="E3", code="stuck")
    s.run_until(lambda s: s.sim.hall_calls[s.labels["f20"]].status == "COMPLETED", 120)
    s.run(20)
    s.say("The technician frees the car. Recovery checks pass (fresh state, no fault, doors clear) and the technician confirms: E3 returns to service.")
    s.act("Technician resolves the stuck-car incident with recovery confirmation.",
          lambda sim: sim.incident_action(inc["incident_id"], "RESOLVE", actor="TECH-CARD-0007", technician_confirmation=True),
          lane="maintenance", to=["emergency", "dispatch"], who="Technician", car="E3", code="resolve",
          why={"kind": "checks", "title": "Recovery checks (MNT-4.6)",
               "items": [{"text": "Fresh valid state from the car", "ok": True}, {"text": "No active fault", "ok": True},
                         {"text": "Doors not obstructed", "ok": True}, {"text": "Technician confirms the result", "ok": True}]})
    s.run(2)
    for p in s.riders("E3"):
        s.act("The passenger in E3 presses 22 again (the old car call was blocked).", lambda sim, p=p: sim.submit_car_call("E3", p.dest),
              car="E3", who="Passenger in E3", code="carcall")
    s.run_until(lambda s: not s.riders("E3"), 60)
    s.run(4)
    return s.finish()


def story_medical() -> dict:
    s = Story("medical", "Medical emergency in a moving car", vt=["VT-20", "VT-27"], mode="Mode 3 · Emergency",
              tagline="E2 is carrying three people up when one of them presses MEDICAL. No card, highest severity, non-stop to floor 1.",
              requirements=["EMR-3.1", "EMR-3.3", "EMR-3.7", "EMR-3.9", "EMR-3.14", "PERF-3"],
              base=datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 4); s.place("E3", 30); s.place("E4", -1); s.place("E5", 11)
    for d in (12, 18, 26):
        s.rider("E2", d)
    s.criterion("MEDICAL accepted without card or role", "EMR-3.1",
                lambda s: any(i.kind == "MEDICAL" for i in s.sim.incidents.values()))
    s.criterion("Critical alert recorded at once with car, floor and direction", "EMR-3.7, PERF-3",
                lambda s: any(i.kind == "MEDICAL" and i.severity == "CRITICAL" and i.floor is not None and i.direction
                              for i in s.sim.incidents.values()))
    s.criterion("Hall calls requeued to other cars; car calls blocked", "EMR-3.3",
                lambda s: "f16" in s.labels and s.sim.hall_calls[s.labels["f16"]].assigned_car not in (None, "E2")
                and any(c.elevator_id == "E2" and c.status.startswith("BLOCKED") for c in s.sim.car_calls.values()))
    s.criterion("E2 runs non-stop to floor 1 and opens its doors there", "EMR-3.14",
                lambda s: (m := s.flags.get("medical_t")) is not None
                and [f for t, f in s.door_opens["E2"] if t > m][:1] == [1])
    s.criterion("Unresolved after 60 s: security notified again", "EMR-3.9",
                lambda s: any(i.kind == "MEDICAL" and i.escalated for i in s.sim.incidents.values()))
    s.criterion("Admin resolves with recovery confirmation; E2 back in service", "EMR-3.9",
                lambda s: any(i.kind == "MEDICAL" and i.status == "RESOLVED" for i in s.sim.incidents.values())
                and s.sim.cars["E2"].availability == "IN_SERVICE")
    s.run(1)
    s.person(16, 29, "f16")
    s.run(4)
    s.say("Between floors 9 and 10, a passenger collapses and someone presses MEDICAL. No card is asked for.")
    s.flags["medical_t"] = s.sim.sim_tick
    inc = s.act("Passenger in E2 presses MEDICAL.", lambda sim: sim.trigger_emergency("E2", "MEDICAL"),
                car="E2", who="Passenger in E2", code="medical")
    s.say("E2 drops floors 12, 18, 26 and the hall call at 16 (requeued to another car). It reverses and runs non-stop to floor 1.",
          on="ResponderRun")
    s.run_until(lambda s: s.sim.cars["E2"].current_floor == 1 and s.sim.cars["E2"].door_state == "OPEN", 60)
    s.run(3)
    s.act("Admin acknowledges the medical incident on the dashboard.",
          lambda sim: sim.incident_action(inc["incident_id"], "ACKNOWLEDGE", actor="ADMIN-CARD-0001"),
          lane="emergency", to=["audit"], who="Admin", car="E2", code="resolve")
    s.say("The paramedics are still with the patient. One minute after the alarm the incident is not resolved: security is notified again.",
          on="Escalation")
    s.run_until(lambda s: s.sim.incidents[inc["incident_id"]].escalated, 120)
    s.run(10)
    s.say("Patient handed over. The Admin resolves the incident; the recovery checks pass and E2 returns to normal dispatch.")
    s.act("Admin resolves the medical incident (recovery confirmed).",
          lambda sim: sim.incident_action(inc["incident_id"], "RESOLVE", actor="ADMIN-CARD-0001", technician_confirmation=True),
          lane="emergency", to=["dispatch", "audit"], who="Admin", car="E2", code="resolve",
          why={"kind": "checks", "title": "Recovery checks (MNT-4.6)",
               "items": [{"text": "Fresh valid state from E2", "ok": True}, {"text": "No active fault", "ok": True},
                         {"text": "Doors not obstructed", "ok": True}, {"text": "Admin confirms the result", "ok": True}]})
    s.run_until(lambda s: s.sim.hall_calls[s.labels["f16"]].status == "COMPLETED" and not any(p.state == "r" for p in s.people.values()), 90)
    s.run(4)
    return s.finish()


def story_rescue() -> dict:
    s = Story("rescue", "Rescue car for a telephone report", vt=["VT-13"], mode="Mode 3 · Emergency",
              tagline="A neighbour phones security: someone on floor 18 is unwell. The Admin records it and sends an empty car that waits with its doors open.",
              requirements=["EMR-3.12", "EMR-3.14", "EMR-3.16", "SEC-2.9"],
              base=datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 6); s.place("E3", 14); s.place("E4", 1); s.place("E5", 18)
    s.criterion("Admin records the phone report as an incident", "EMR-3.16",
                lambda s: any(i.kind == "MEDICAL" and i.trigger_source == "ADMIN_MANUAL" for i in s.sim.incidents.values()))
    s.criterion("Rescue car is an eligible car by ETA — never E5, even though E5 is at floor 18", "EMR-3.12",
                lambda s: any(i.rescue_car_id == "E3" for i in s.sim.incidents.values()) or s.flags.get("rescued_by") == "E3")
    s.criterion("The rescue car's pending hall call is requeued to another car", "EMR-3.12",
                lambda s: "f25" in s.labels and s.sim.hall_calls[s.labels["f25"]].reassignment_count >= 1
                and s.sim.hall_calls[s.labels["f25"]].assigned_car != "E3")
    s.criterion("Doors held open for the Admin-set wait, without a door fault", "EMR-3.16",
                lambda s: s.flags.get("held_open", 0) >= 10 and not any(i.kind == "DOOR_OBSTRUCTION" for i in s.sim.incidents.values()))
    s.criterion("After boarding is confirmed, non-stop run to floor 1", "EMR-3.14",
                lambda s: (b := s.flags.get("board_t")) is not None and [f for t, f in s.door_opens["E3"] if t > b][:1] == [1])
    s.run(1)
    s.person(25, 2, "f25")
    s.run(2)
    s.say("Security takes a phone call. There is no button press to react to, so the Admin records the medical emergency on the dashboard.")
    inc = s.act("Admin records a medical emergency on floor 18, reported by telephone.",
                lambda sim: sim.phone_medical_incident(18), lane="emergency", to=["audit"], who="Admin", code="phone")
    ranking = []
    for eid in ("E1", "E2", "E3", "E4"):
        car = s.sim.cars[eid]
        ranking.append({"car": eid, "eta": s.sim._eta_and_insertion(car, 18, None)[0], "penalty": 0, "cost": s.sim._eta_and_insertion(car, 18, None)[0],
                        "index": 0, "floors_month": 0, "floor": car.current_floor, "direction": car.direction, "stops": len(car.stops)})
    s.say("The Admin sends a rescue car and sets a 45 s door hold. E5 is right at floor 18 but is never a rescue car. E3 is closest; its call at 25 goes back to the queue.")
    s.act("Admin dispatches the rescue car (car confirmed empty, wait 45 s).",
          lambda sim: sim.dispatch_rescue(inc["incident_id"], wait_seconds=45, confirmed_empty=True),
          lane="emergency", to=["dispatch", "stub"], who="Admin", code="rescue",
          why={"kind": "decision", "chosen": min(ranking, key=lambda r: (r["eta"], r["car"]))["car"], "candidates": ranking,
               "excluded": [{"car": "E5", "reason": "never a rescue car"}], "cap": 0, "balancing": False,
               "floor": 18, "direction": None, "call_type": "RESCUE"})
    s.flags["rescued_by"] = s.sim.incidents[inc["incident_id"]].rescue_car_id
    s.run_until(lambda s: s.sim.cars["E3"].current_floor == 18 and s.sim.cars["E3"].door_state == "OPEN", 60)
    patient = s._new_person(18, 1, "patient")
    helper = s._new_person(18, 1, "helper")
    for k in range(12):
        s.run(1)
        if s.sim.cars["E3"].door_state == "OPEN":
            s.flags["held_open"] = s.flags.get("held_open", 0) + 1
        if k == 6:
            for p in (patient, helper):
                p.state, p.car = "r", "E3"
            s._emit({"type": "PassengerBoard", "lane": "stub", "to": [], "car": "E3", "text": "The patient and a helper board E3."})
    s.say("The Admin sees them aboard on the camera feed and confirms. E3 becomes the affected car and runs non-stop to floor 1.")
    s.flags["board_t"] = s.sim.sim_tick
    s.act("Admin confirms the patient is aboard.", lambda sim: sim.confirm_rescue_boarding(inc["incident_id"]),
          lane="emergency", to=["stub", "audit"], who="Admin", car="E3", code="rescue_board")
    s.run_until(lambda s: s.sim.cars["E3"].current_floor == 1 and s.sim.cars["E3"].door_state == "OPEN", 60)
    s.run(4)
    s.act("Admin resolves the incident after the hand-over (recovery confirmed).",
          lambda sim: sim.incident_action(inc["incident_id"], "RESOLVE", actor="ADMIN-CARD-0001", technician_confirmation=True),
          lane="emergency", to=["dispatch", "audit"], who="Admin", car="E3", code="resolve")
    s.run_until(lambda s: s.sim.hall_calls[s.labels["f25"]].status == "COMPLETED" and not any(p.state == "r" for p in s.people.values()), 90)
    s.run(4)
    return s.finish()


def story_obstruction() -> dict:
    s = Story("obstruction", "Door obstruction", vt=["VT-06", "VT-22"], mode="Mode 3 · Safety",
              tagline="A suitcase is stuck in E1's door at floor 7. The close command is refused, the car stays put, and after 10 s security is warned.",
              requirements=["FR-1.15", "EMR-3.10", "MNT-4.10", "HW-2"],
              base=datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc))
    s.place("E1", 2); s.place("E2", 22); s.place("E3", 30); s.place("E4", 26); s.place("E5", 10)
    st: dict = {}
    s.criterion("Close command refused while the obstruction is active", "FR-1.15",
                lambda s: st.get("close_refused") is True)
    s.criterion("Door never reported closed and car never moves while obstructed", "FR-1.15",
                lambda s: not (s.sim.cars["E1"].obstruction_started is not None and
                               (s.sim.cars["E1"].door_state != "OBSTRUCTED" or s.sim.cars["E1"].current_floor != 7)), kind="always")
    s.criterion("After 10 s a warning incident is raised", "FR-1.15",
                lambda s: any(i.kind == "DOOR_OBSTRUCTION" for i in s.sim.incidents.values()))
    s.criterion("Once cleared, doors close first, then the car continues", "FR-1.16",
                lambda s: st.get("cleared") is not None and s.sim.cars["E1"].current_floor > 7)
    s.run(1)
    s.person(7, 20, "f7")
    s.run_until(lambda s: s.sim.cars["E1"].current_floor == 7 and s.sim.cars["E1"].door_state == "OPEN", 30)
    s.say("A passenger drags a suitcase in; a wheel jams the door at floor 7. The door sensor reports an obstruction.")
    s.act("Door sensor: obstruction on E1.", lambda sim: sim.door_obstruction("E1", True), car="E1",
          who="Door sensor", code="obstruction")
    s.run(1)
    s.say("Another passenger presses Close. It is refused: a close command is never issued against an obstruction.")
    s.act("A passenger presses door close.", lambda sim: sim.passenger_door_command("E1", "CLOSE"),
          car="E1", who="Passenger in E1", code="door", expect_error=True)
    st["close_refused"] = s.sim.commands[-1]["status"] == "REJECTED"
    s.say("Ten seconds and still blocked: a warning incident opens and security is told. The car stays at floor 7 with its pending stop.")
    s.run_until(lambda s: any(i.kind == "DOOR_OBSTRUCTION" for i in s.sim.incidents.values()), 20)
    s.run(5)
    s.say("The suitcase is freed. The doors stay open briefly, close, and only then does E1 continue to 20.")
    st["cleared"] = s.sim.sim_tick
    s.act("Door sensor: obstruction cleared.", lambda sim: sim.door_obstruction("E1", False), car="E1",
          who="Door sensor", code="obstruction")
    s.run_until(lambda s: not s.riders("E1") and not s.sim.cars["E1"].stops, 60)
    inc = next(i for i in s.sim.incidents.values() if i.kind == "DOOR_OBSTRUCTION")
    s.act("Admin closes the obstruction incident.", lambda sim: (sim.incident_action(inc.incident_id, "RESOLVE", actor="ADMIN-CARD-0001", technician_confirmation=True),
                                                                sim.incident_action(inc.incident_id, "CLOSE", actor="ADMIN-CARD-0001")),
          lane="emergency", to=["audit"], who="Admin", car="E1", code="resolve")
    s.run(4)
    return s.finish()


def story_comms() -> dict:
    s = Story("comms", "Communication loss and recovery", vt=["VT-08", "VT-28"], mode="Mode 4 · Reliability",
              tagline="E4 stops answering mid-trip. SREMS marks it stale, keeps every request, never replays an uncertain command, and reconciles before control resumes.",
              requirements=["MNT-4.8", "MNT-4.9", "COM-3", "COM-4", "FR-1.6", "FR-1.17"],
              base=datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 1); s.place("E3", 30); s.place("E4", 8); s.place("E5", 20)
    s.rider("E4", 25)
    st: dict = {}
    s.criterion("E4 marked STALE and excluded from dispatch", "MNT-4.8, FR-1.6",
                lambda s: st.get("stale_seen") is True)
    s.criterion("In-flight command recorded as UNKNOWN", "COM-3",
                lambda s: any(c["status"] == "UNKNOWN" and c["elevator_id"] == "E4" for c in s.sim.commands))
    s.criterion("Hall call moved to another car; car call BLOCKED, not completed", "MNT-4.8",
                lambda s: "f15" in s.labels and s.sim.hall_calls[s.labels["f15"]].assigned_car not in (None, "E4")
                and st.get("blocked_seen") is True)
    s.criterion("Commands to the stale car are refused", "MNT-4.8",
                lambda s: st.get("stale_refused") is True)
    s.criterion("After reconnecting: fresh snapshot, unknown outcome FAILED (no blind retry)", "MNT-4.9, COM-4",
                lambda s: any(c.elevator_id == "E4" and c.status == "FAILED" for c in s.sim.car_calls.values()))
    s.criterion("E4 returns to service only after reconciliation", "MNT-4.9",
                lambda s: st.get("reconciled") is True and s.sim.cars["E4"].availability == "IN_SERVICE")
    s.run(1)
    s.person(15, 28, "f15")
    s.run(3)
    s.say("Between floors 11 and 12 the link to E4's controller drops. SREMS cannot confirm its state any more.")
    s.act("Interface: E4 connection lost (heartbeat timeout).", lambda sim: sim.communication_loss("E4"),
          car="E4", who="Elevator interface", code="comms")
    st["stale_seen"] = s.sim.cars["E4"].availability == "STALE" and not s.sim._eligible(s.sim.cars["E4"])
    st["blocked_seen"] = any(c.elevator_id == "E4" and c.status.startswith("BLOCKED") for c in s.sim.car_calls.values())
    s.run(3)
    s.say("Someone tries to send E4 a door command. With the state unconfirmed, no new command is sent.")
    s.act("Door-open command for E4.", lambda sim: sim.passenger_door_command("E4", "OPEN"),
          car="E4", who="Operator", code="door", expect_error=True)
    st["stale_refused"] = s.sim.commands[-1]["status"] == "REJECTED"
    s.run(14)
    s.say("The link is back. The technician reconciles: a fresh snapshot first; the car call whose outcome is unknown is marked FAILED, never replayed. Then E4 rejoins dispatch.")
    s.act("Technician reconciles E4 after reconnection.", lambda sim: sim.reconcile_connection("E4", actor="TECH-CARD-0007"),
          lane="maintenance", to=["dispatch", "audit"], who="Technician", car="E4", code="reconcile")
    st["reconciled"] = True
    s.run(2)
    s.act("The passenger in E4 presses 25 again.", lambda sim: sim.submit_car_call("E4", 25), car="E4",
          who="Passenger in E4", code="carcall")
    s.run_until(lambda s: not any(p.state == "r" for p in s.people.values())
                and s.sim.hall_calls[s.labels["f15"]].status == "COMPLETED", 90)
    s.run(4)
    return s.finish()


def story_maintenance() -> dict:
    s = Story("maintenance", "Maintenance isolation and a blocking fault", vt=["VT-09"], mode="Mode 4 · Maintenance",
              tagline="E2 is taken out for maintenance while it approaches a call; later E4 reports a blocking fault. Other cars keep working throughout.",
              requirements=["MNT-4.1", "MNT-4.3", "MNT-4.4", "MNT-4.6", "FR-1.18", "NFR-09"],
              base=datetime(2026, 11, 23, 10, 30, tzinfo=timezone.utc))
    s.place("E1", 30); s.place("E2", 6); s.place("E3", 28); s.place("E4", 1); s.place("E5", 14)
    st: dict = {}
    s.criterion("Car in maintenance gets no new assignments", "MNT-4.4",
                lambda s: all(d["chosen"] != "E2" for d in s.sim.decision_trace if d["tick"] >= st.get("maint_t", 10**9)
                              and d["tick"] < st.get("back_t", 10**9)), kind="always")
    s.criterion("Hall call reassigned; original acceptance time kept", "FR-1.18",
                lambda s: "f16" in s.labels and (r := s.sim.hall_calls[s.labels["f16"]]).reassignment_count == 1
                and r.wait_seconds is not None and r.accepted_at == s.sim.base_time + timedelta(seconds=1))
    s.criterion("Other cars keep receiving assignments", "NFR-09",
                lambda s: any(d["tick"] >= st.get("maint_t", 10**9) and d["chosen"] in ("E1", "E3", "E4") for d in s.sim.decision_trace))
    s.criterion("Return to service refused while the fault is active", "MNT-4.6",
                lambda s: st.get("refused") is True)
    s.criterion("After the fault clears, the confirmed return succeeds and is logged", "MNT-4.6, MNT-4.7",
                lambda s: s.sim.cars["E4"].availability == "IN_SERVICE" and st.get("fault_t") is not None
                and any(c["command_type"] == "RETURN_TO_SERVICE" and c["elevator_id"] == "E4" and c["status"] == "ACKNOWLEDGED"
                        for c in s.sim.commands))
    s.run(1)
    s.person(16, 3, "f16")
    s.run(4)
    s.say("E2 is on its way to floor 16. The technician puts it in maintenance from the service control. Its call goes back to the queue and another car takes it.")
    st["maint_t"] = s.sim.sim_tick
    s.act("Technician TECH-CARD-0007 puts E2 in maintenance.", lambda sim: sim.isolate("E2", "MAINTENANCE", technician_id="TECH-CARD-0007"),
          lane="maintenance", to=["dispatch", "audit"], who="Technician", car="E2", code="isolate")
    s.run(3)
    s.person(9, 27, "f9")
    s.run_until(lambda s: s.sim.hall_calls[s.labels["f16"]].status == "COMPLETED", 90)
    s.say("Maintenance done. Recovery checks pass and the technician returns E2 to service.")
    st["back_t"] = s.sim.sim_tick
    s.act("Technician returns E2 to service (checks confirmed).", lambda sim: sim.isolate("E2", "IN_SERVICE", technician_id="TECH-CARD-0007"),
          lane="maintenance", to=["dispatch", "audit"], who="Technician", car="E2", code="return",
          why={"kind": "checks", "title": "Recovery checks (MNT-4.6)",
               "items": [{"text": "Fresh valid state", "ok": True}, {"text": "No active fault", "ok": True},
                         {"text": "Doors not obstructed", "ok": True}, {"text": "Technician confirms", "ok": True}]})
    s.run(6)
    s.say("E4's drive reports an over-temperature fault: a blocking fault. E4 is isolated and an incident opens.")
    st["fault_t"] = s.sim.sim_tick
    inc = s.act("E4 drive reports fault MOTOR_OVERTEMP.", lambda sim: sim.report_fault("E4", "MOTOR_OVERTEMP"),
                car="E4", who="E4 drive", code="fault")
    s.run(4)
    s.say("Someone tries to return E4 too early. Recovery check 'no active fault' fails, so the command is rejected and logged.")
    s.act("Technician tries to return E4 while the fault is active.", lambda sim: sim.isolate("E4", "IN_SERVICE", technician_id="TECH-CARD-0007"),
          lane="maintenance", to=["audit"], who="Technician", car="E4", code="return", expect_error=True,
          why={"kind": "checks", "title": "Recovery checks (MNT-4.6)",
               "items": [{"text": "Fresh valid state", "ok": True}, {"text": "No active fault", "ok": False},
                         {"text": "Doors not obstructed", "ok": True}, {"text": "Technician confirms", "ok": True}]})
    st["refused"] = s.sim.commands[-1]["status"] == "REJECTED"
    s.run(8)
    s.act("E4 drive: fault cleared after cooling.", lambda sim: sim.clear_fault("E4"), car="E4", who="E4 drive", code="fault")
    s.run(2)
    s.act("Technician resolves the fault incident with confirmation; E4 returns to service.",
          lambda sim: sim.incident_action(inc["incident_id"], "RESOLVE", actor="TECH-CARD-0007", technician_confirmation=True),
          lane="maintenance", to=["dispatch", "audit"], who="Technician", car="E4", code="resolve",
          why={"kind": "checks", "title": "Recovery checks (MNT-4.6)",
               "items": [{"text": "Fresh valid state", "ok": True}, {"text": "No active fault", "ok": True},
                         {"text": "Doors not obstructed", "ok": True}, {"text": "Technician confirms", "ok": True}]})
    s.run(2)
    for p in s.riders("E4"):
        s.act(f"The passenger in E4 presses {floor_name(p.dest)} again (the blocked call was not replayed).",
              lambda sim, p=p: sim.submit_car_call("E4", p.dest), car="E4", who="Passenger in E4", code="carcall")
    s.run_until(lambda s: not any(p.state == "r" for p in s.people.values()) and not any(p.state == "w" for p in s.people.values()), 90)
    s.run(4)
    return s.finish()


def story_freight() -> dict:
    s = Story("freight", "E5 service run and freight calls", vt=["VT-14", "VT-03"], mode="Mode 1 · Freight",
              tagline="Building staff take E5 non-stop from 3 to 24 with a delivery. While it is OCCUPIED, freight calls are refused, not queued.",
              requirements=["FR-1.13", "FR-1.10", "UC-12"],
              base=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc))
    s.place("E1", 1); s.place("E2", 12); s.place("E3", 20); s.place("E4", 28); s.place("E5", 3)
    st: dict = {}
    s.criterion("E5 shows OCCUPIED for the whole service run", "FR-1.13",
                lambda s: st.get("occupied_seen") is True)
    s.criterion("Service run goes straight to 24 without stopping", "FR-1.13",
                lambda s: (t0 := st.get("run_t")) is not None and [f for t, f in s.door_opens["E5"] if t > t0][:1] == [24])
    s.criterion("Freight call during the run is rejected with a message, not queued", "FR-1.13",
                lambda s: st.get("freight_refused") is True and not any(r.call_type == "FREIGHT" and r.origin_floor == 10 and r.status == "QUEUED"
                                                                       for r in s.sim.hall_calls.values()))
    s.criterion("E5 never serves B1", "FR-1.10",
                lambda s: st.get("b1_refused") is True)
    s.criterion("No ordinary hall call is ever assigned to E5", "FR-1.4",
                lambda s: all(d["chosen"] != "E5" for d in s.sim.decision_trace if d["call_type"] == "ORDINARY"), kind="always")
    s.criterion("After the run E5 takes a resident's freight call", "FR-1.13",
                lambda s: any(r.call_type == "FREIGHT" and r.status == "COMPLETED" for r in s.sim.hall_calls.values()))
    s.run(1)
    s.say("Building-service staff authenticate at E5's service control and start a run from 3 to 24 with a furniture delivery. E5 becomes OCCUPIED.")
    st["run_t"] = s.sim.sim_tick
    staff = s._new_person(3, 24, "staff")
    staff.state, staff.car = "r", "E5"
    s.act("Staff start an E5 service run to floor 24.", lambda sim: sim.start_service_run(24, credential_id="STAFF-CARD-0003"),
          lane="dispatch", to=["stub", "audit"], who="Building-service staff", car="E5", code="service")
    st["occupied_seen"] = s.sim.cars["E5"].availability == "OCCUPIED"
    s.run(4)
    s.say("A resident on floor 10 presses the freight key. E5 is OCCUPIED: the call is rejected with a message on the hall panel and is not queued.")
    s.act("Resident presses the E5 freight key on floor 10.",
          lambda sim: sim.submit_hall_call(origin_floor=10, call_type="FREIGHT"), who="Resident on floor 10",
          lane="stub", to=["dispatch"], code="hall", expect_error=True)
    st["freight_refused"] = s.sim.commands[-1]["status"] == "REJECTED"
    s.run(2)
    s.person(10, 1, "f10")
    s.run(3)
    s.act("Someone tries a freight call from B1.", lambda sim: sim.submit_hall_call(origin_floor=-1, call_type="FREIGHT"),
          who="Resident at B1", lane="stub", to=["dispatch"], code="hall", expect_error=True)
    st["b1_refused"] = s.sim.commands[-1]["status"] == "REJECTED"
    for _ in range(30):
        s.run(1)
        if s.sim.cars["E5"].availability != "OCCUPIED" and s.sim.cars["E5"].current_floor != 24:
            st["occupied_seen"] = False
        if s.sim.cars["E5"].current_floor == 24 and s.sim.cars["E5"].door_state == "OPEN":
            break
    s.run(4)
    s.say("Run complete: E5 is back in service and now accepts the resident's freight call (goods, pets, furniture).")
    p = s._new_person(10, 2, "res")
    def freight(sim):
        req, _ = sim.submit_hall_call(origin_floor=10, call_type="FREIGHT")
        p.req = req["request_id"]
        return req
    s.act("Resident on floor 10 presses the E5 freight key again.", freight, who="Resident on floor 10",
          lane="stub", to=["dispatch"], code="hall")
    s.run_until(lambda s: not any(p.state in ("w", "r") for p in s.people.values()), 120)
    s.run(4)
    return s.finish()


def story_fire() -> dict:
    s = Story("fire", "Fire alarm with a stuck car", vt=["VT-27", "VT-20"], mode="Mode 3 · Emergency",
              tagline="E3 is already stuck when the fire alarm goes off. The building-wide policy wins: every car stops, nothing is recalled, and the stuck car stays out afterwards.",
              requirements=["EMR-3.6", "EMR-3.13", "EMR-3.3", "EMR-3.14", "UI-7"],
              base=datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc))
    s.place("E1", 3); s.place("E2", 14); s.place("E3", 22); s.place("E4", 9); s.place("E5", 17)
    s.rider("E1", 26)
    s.rider("E4", 1)
    st: dict = {}
    s.criterion("Fire: every car EMERGENCY_UNAVAILABLE at once", "EMR-3.6",
                lambda s: st.get("all_emergency") is True)
    s.criterion("No car moves while the fire policy is active (no recall)", "EMR-3.6",
                lambda s: not st.get("fire_on") or all(c.current_floor == st["frozen"][eid] for eid, c in s.sim.cars.items()), kind="always")
    s.criterion("Requests are kept (queued or blocked), never dropped", "EMR-3.3",
                lambda s: not st.get("fire_on") or "f12" not in s.labels or s.sim.hall_calls[s.labels["f12"]].status == "QUEUED", kind="always")
    s.criterion("MEDICAL during the fire: the car does not run", "VT-20, EMR-3.13",
                lambda s: st.get("medical_no_run") is True)
    s.criterion("Fire resolved: cars without other incidents return; queued call served", "EMR-3.6",
                lambda s: st.get("fire_resolved") is True and s.sim.cars["E1"].availability == "IN_SERVICE"
                and "f12" in s.labels and s.sim.hall_calls[s.labels["f12"]].status == "COMPLETED")
    s.criterion("Stuck car E3 stays out with its incident open", "EMR-3.13",
                lambda s: st.get("fire_resolved") and s.sim.cars["E3"].availability == "EMERGENCY_UNAVAILABLE"
                and any(i.kind == "STUCK_CAR" and i.status == "OPEN" for i in s.sim.incidents.values()), kind="final")
    s.run(1)
    s.say("Evening. E3 has stopped at floor 22 with someone inside; the Admin has confirmed it stuck. E1 and E4 are carrying passengers.")
    help_inc = s.act("Passenger in E3 presses HELP.", lambda sim: sim.trigger_emergency("E3", "HELP"), car="E3",
                     who="Passenger in E3", code="help")
    s.act("Admin confirms E3 is stuck.", lambda sim: sim.confirm_help_as_stuck(help_inc["incident_id"], actor="ADMIN-CARD-0001"),
          lane="emergency", to=["dispatch", "audit"], who="Admin", car="E3", code="stuck")
    s.run(3)
    s.say("The fire alarm panel reports a fire. The default building-emergency policy applies to every car: EMERGENCY_UNAVAILABLE, no new assignments, and no recall movement.")
    fire = s.act("Building alarm system: FIRE.", lambda sim: sim.building_emergency("FIRE"),
                 lane="stub", to=["emergency", "dispatch", "audit"], who="Fire alarm panel", code="fire")
    st["fire_on"] = True
    st["frozen"] = {eid: c.current_floor for eid, c in s.sim.cars.items()}
    st["all_emergency"] = all(c.availability == "EMERGENCY_UNAVAILABLE" for c in s.sim.cars.values())
    s.run(3)
    s.say("A resident on floor 12 still presses the hall button. The call is accepted and kept in the queue; nothing is assigned during the fire.")
    s.person(12, 1, "f12")
    s.run(4)
    s.say("Someone in E2 presses MEDICAL during the fire. The incident is recorded with highest severity, but the fire ranks higher: the car does not run (EMR-3.13).")
    s.act("Passenger in E2 presses MEDICAL.", lambda sim: sim.trigger_emergency("E2", "MEDICAL"), car="E2",
          who="Passenger in E2", code="medical")
    st["medical_no_run"] = not s.sim.cars["E2"].medical_run
    s.run(15)
    s.say("Fire brigade gives the all-clear. Resolving the fire returns every car that has no incident of its own. E3 (stuck) and E2 (medical) stay out.")
    s.act("Admin resolves the fire incident.", lambda sim: sim.incident_action(fire["incident_id"], "RESOLVE", actor="ADMIN-CARD-0001", technician_confirmation=True),
          lane="emergency", to=["dispatch", "audit"], who="Admin", code="resolve")
    st["fire_on"] = False
    st["fire_resolved"] = True
    s.run(2)
    for eid in ("E1", "E4"):
        for p in s.riders(eid):
            s.act(f"The passenger in {eid} presses {floor_name(p.dest)} again.", lambda sim, eid=eid, p=p: sim.submit_car_call(eid, p.dest),
                  car=eid, who=f"Passenger in {eid}", code="carcall")
    s.run_until(lambda s: s.sim.hall_calls[s.labels["f12"]].status == "COMPLETED"
                and not any(p.state == "r" and p.car in ("E1", "E4") for p in s.people.values()), 120)
    s.run(4)
    return s.finish()


STORIES: list[Callable[[], dict]] = [story_dispatch, story_wear, story_peak, story_calls, story_help,
                                     story_medical, story_rescue, story_obstruction, story_comms,
                                     story_maintenance, story_freight, story_fire]


def record_all() -> dict:
    stories = [fn() for fn in STORIES]
    return {
        "generatedBy": "python -m simulation.story_recorder",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "simulation/operational.py (labelled software test stub, SWI-2)",
        "floors": [-1] + list(range(1, 31)),
        "cars": list(OperationalSimulator.ELEVATOR_IDS),
        "capacity": CAPACITY,
        "code": CODE,
        "stories": stories,
    }


def record_one(key: str) -> dict:
    for fn in STORIES:
        if fn.__name__ == f"story_{key}":
            return fn()
    raise KeyError(key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="frontend/public/player/stories.js")
    args = parser.parse_args()
    data = record_all()
    text = ("// Generated by `python -m simulation.story_recorder` from real simulator runs. Do not edit.\n"
            "window.SREMS_STORIES = " + json.dumps(data, separators=(",", ":"), default=str) + ";\n")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    for st in data["stories"]:
        passed = sum(1 for c in st["criteria"] if c["passed_at"] is not None)
        print(f"{st['key']:12s} {st['until']:4d} s  {len(st['events']):4d} events  criteria {passed}/{len(st['criteria'])}")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
