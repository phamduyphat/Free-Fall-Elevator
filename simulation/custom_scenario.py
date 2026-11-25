"""Custom validation scenarios: you choose how many people, where they start, where they go,
when they arrive, and which incidents happen; the real simulator runs it.

The same scenario (same people, same seed) is run under three dispatch rules so you can see
whether the SREMS dispatch is actually doing better than simple alternatives:

* ``SREMS``         FR-1.4 as built: predicted service time + wear penalty (≤ 15 s, paused when busy).
* ``SREMS_NO_WEAR`` the same without the wear penalty: the price of balancing wear.
* ``NEAREST``       a naive baseline: the physically closest car, ignoring direction and queue.

Only the SREMS run is recorded frame by frame for the scenario player; the other two give
the comparison figures.

Usage::

    python -m simulation.custom_scenario my_test.json                 # print the report
    python -m simulation.custom_scenario my_test.json --player        # also add it to the player
    python -m simulation.custom_scenario --example > my_test.json     # a spec to start from

Spec (JSON)::

    {
      "name": "Monday 08:00 rush",
      "start": "07:55",                 # time of day (UTC, simulated) or ISO datetime
      "seed": 7,                        # same seed -> same people -> comparable runs
      "cars": {"E1": {"floor": 1}, "E2": {"floor": 15, "state": "MAINTENANCE"},
               "E3": {"floor": 20, "floors_month": 456000}},   # trip counter this month
      "groups": [
        {"count": 40, "from": "random", "to": "lobby", "start": 0, "over": 180},
        {"count": 6,  "from": "lobby",  "to": [12, 18, 25], "start": 60, "over": 60},
        {"count": 2,  "from": 10, "to": 2, "start": 30, "over": 0, "kind": "freight"}
      ],
      "incidents": [
        {"type": "MEDICAL", "car": "E3", "at": 45, "duration": 60},
        {"type": "FAULT", "car": "E4", "at": 90, "duration": 40}
      ],
      "targets": {"avg_wait": 60, "p95_wait": 120}
    }

Floors: -1 is B1, 1 is the lobby, 2..30 residential. ``from``/``to`` take a floor number, a
list of floors (one is drawn per person), or a word: ``lobby``, ``B1``, ``random`` (2..30),
``lower`` (2..15), ``upper`` (16..30), ``any`` (B1, 1..30). Incident types: ``MEDICAL``,
``HELP``, ``OBSTRUCTION``, ``MAINTENANCE``, ``FAULT``; each ends after ``duration`` seconds
(the Admin or technician resolves it, the car returns to service).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from pathlib import Path
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from simulation.operational import OperationalError
from simulation.story_recorder import CODE, CAPACITY, Story, floor_name
from simulation.wear import WearConfig

CARS = ("E1", "E2", "E3", "E4", "E5")
FLOORS = [-1] + list(range(1, 31))
WORDS = {
    "lobby": [1], "b1": [-1], "random": list(range(2, 31)), "lower": list(range(2, 16)),
    "upper": list(range(16, 31)), "any": FLOORS,
}
INCIDENTS = ("MEDICAL", "HELP", "OBSTRUCTION", "MAINTENANCE", "FAULT")
POLICIES = [
    ("SREMS", "SREMS dispatch (as built)", "Predicted service time plus a wear penalty of at most 15 s, paused while the group is busy (FR-1.4, WEAR-3)."),
    ("SREMS_NO_WEAR", "SREMS without wear balancing", "Same cost function, wear penalty switched off: shows what balancing costs."),
    ("NEAREST", "Nearest car (baseline)", "Physically closest car, ignoring its direction and queued stops."),
]
LIMITS = {"people": 600, "groups": 30, "incidents": 12, "window": 3600, "drain": 1800}


class SpecError(ValueError):
    """The scenario description is not valid; the message says which field and why."""


# ------------------------------------------------------------------------------- spec
def _floors(value: Any, field: str) -> list[int]:
    if isinstance(value, bool):
        raise SpecError(f"{field}: expected a floor, a list of floors or a word")
    if isinstance(value, int):
        vals = [value]
    elif isinstance(value, str):
        key = value.strip().lower()
        if key in WORDS:
            return WORDS[key]
        if re.fullmatch(r"-?\d+", key):
            vals = [int(key)]
        else:
            raise SpecError(f"{field}: '{value}' is not a floor or one of {', '.join(WORDS)}")
    elif isinstance(value, list) and value:
        vals = []
        for v in value:
            vals.extend(_floors(v, field))
    else:
        raise SpecError(f"{field}: expected a floor, a list of floors or a word")
    bad = [v for v in vals if v not in FLOORS]
    if bad:
        raise SpecError(f"{field}: floor {bad[0]} does not exist (B1 = -1, then 1..30)")
    return vals


def _start(value: Any) -> datetime:
    if value in (None, ""):
        return datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
    text = str(value).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        h, mi, se = int(m[1]), int(m[2]), int(m[3] or 0)
        if h > 23 or mi > 59 or se > 59:
            raise SpecError(f"start: '{text}' is not a time of day")
        return datetime(2026, 11, 23, h, mi, se, tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SpecError(f"start: '{text}' is not HH:MM or an ISO date-time") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _int(d: dict, key: str, default: int, lo: int, hi: int, where: str) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise SpecError(f"{where}.{key}: expected a whole number")
    v = int(v)
    if not lo <= v <= hi:
        raise SpecError(f"{where}.{key}: must be between {lo} and {hi}")
    return v


def normalise(spec: dict) -> dict:
    """Validate a spec and fill defaults. Raises SpecError with a field-specific message."""
    if not isinstance(spec, dict):
        raise SpecError("The scenario must be a JSON object")
    out: dict[str, Any] = {
        "name": str(spec.get("name") or "Custom scenario")[:80],
        "start": _start(spec.get("start")).isoformat(),
        "seed": _int(spec, "seed", 1, 0, 10**9, "spec"),
    }
    cars = spec.get("cars") or {}
    if not isinstance(cars, dict):
        raise SpecError("cars: expected an object like {\"E1\": {\"floor\": 1}}")
    default_floor = {"E1": 1, "E2": 8, "E3": 16, "E4": 24, "E5": 1}
    out["cars"] = {}
    for eid in CARS:
        c = cars.get(eid) or {}
        if not isinstance(c, dict):
            raise SpecError(f"cars.{eid}: expected an object")
        floor = _floors(c.get("floor", default_floor[eid]), f"cars.{eid}.floor")[0]
        if eid == "E5" and floor == -1:
            raise SpecError("cars.E5.floor: E5 does not serve B1 (FR-1.10)")
        state = str(c.get("state", "IN_SERVICE")).upper().replace(" ", "_")
        if state not in ("IN_SERVICE", "MAINTENANCE"):
            raise SpecError(f"cars.{eid}.state: IN_SERVICE or MAINTENANCE")
        month = _int(c, "floors_month", 78_000 if eid == "E5" else 465_000, 0, 10**8, f"cars.{eid}")
        out["cars"][eid] = {"floor": floor, "state": state, "floors_month": month}
    if all(out["cars"][e]["state"] == "MAINTENANCE" for e in CARS[:4]):
        raise SpecError("cars: at least one of E1–E4 must be in service, or nobody can be carried")

    groups = spec.get("groups")
    if not isinstance(groups, list) or not groups:
        raise SpecError("groups: add at least one group of people")
    if len(groups) > LIMITS["groups"]:
        raise SpecError(f"groups: at most {LIMITS['groups']}")
    out["groups"], total = [], 0
    for k, g in enumerate(groups, 1):
        where = f"groups[{k}]"
        if not isinstance(g, dict):
            raise SpecError(f"{where}: expected an object")
        kind = str(g.get("kind", "resident")).lower()
        if kind not in ("resident", "freight"):
            raise SpecError(f"{where}.kind: resident or freight")
        frm = _floors(g.get("from", "random"), f"{where}.from")
        to = _floors(g.get("to", "lobby"), f"{where}.to")
        if kind == "freight" and (-1 in frm or -1 in to):
            raise SpecError(f"{where}: freight goes by E5, which does not serve B1 (FR-1.10)")
        if len(set(frm) | set(to)) == 1:
            raise SpecError(f"{where}: 'from' and 'to' are the same single floor")
        count = _int(g, "count", 1, 1, LIMITS["people"], where)
        total += count
        out["groups"].append({
            "label": str(g.get("label") or "")[:40], "count": count, "kind": kind,
            "from": g.get("from", "random"), "to": g.get("to", "lobby"), "_from": frm, "_to": to,
            "start": _int(g, "start", 0, 0, LIMITS["window"], where),
            "over": _int(g, "over", 0, 0, LIMITS["window"], where),
        })
    if total > LIMITS["people"]:
        raise SpecError(f"groups: {total} people in total; at most {LIMITS['people']}")

    incs = spec.get("incidents") or []
    if not isinstance(incs, list) or len(incs) > LIMITS["incidents"]:
        raise SpecError(f"incidents: a list of at most {LIMITS['incidents']}")
    out["incidents"] = []
    for k, inc in enumerate(incs, 1):
        where = f"incidents[{k}]"
        if not isinstance(inc, dict):
            raise SpecError(f"{where}: expected an object")
        typ = str(inc.get("type", "")).upper()
        if typ not in INCIDENTS:
            raise SpecError(f"{where}.type: one of {', '.join(INCIDENTS)}")
        car = str(inc.get("car", "")).upper()
        if car not in CARS:
            raise SpecError(f"{where}.car: one of {', '.join(CARS)}")
        out["incidents"].append({"type": typ, "car": car,
                                 "at": _int(inc, "at", 30, 0, LIMITS["window"], where),
                                 "duration": _int(inc, "duration", 60, 5, LIMITS["window"], where)})
    out["incidents"].sort(key=lambda i: i["at"])

    tg = spec.get("targets") or {}
    out["targets"] = {"avg_wait": _int(tg, "avg_wait", 60, 1, 3600, "targets"),
                      "p95_wait": _int(tg, "p95_wait", 120, 1, 3600, "targets")}
    return out


def plan_people(spec: dict) -> list[dict]:
    """Who appears when, where they start and where they go: fixed by the seed, so every
    dispatch rule is tested against exactly the same people."""
    rng = random.Random(spec["seed"])
    people = []
    for gi, g in enumerate(spec["groups"]):
        for k in range(g["count"]):
            at = g["start"] + (round(rng.uniform(0, g["over"])) if g["over"] else 0)
            for _ in range(50):
                a, b = rng.choice(g["_from"]), rng.choice(g["_to"])
                if a != b:
                    break
            else:
                raise SpecError(f"groups[{gi + 1}]: cannot draw different start and destination floors")
            people.append({"at": at, "from": a, "to": b, "kind": g["kind"], "group": gi})
    people.sort(key=lambda p: (p["at"], p["group"]))
    return people


# ------------------------------------------------------------------------------- run
def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(p * len(v)) - 1)]


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def run(spec: dict, policy: str = "SREMS", record: bool = True,
        sim_options: dict | None = None) -> tuple[Story, dict]:
    people = plan_people(spec)
    base = datetime.fromisoformat(spec["start"])
    total = len(people)
    window = max([p["at"] for p in people] + [i["at"] + i["duration"] for i in spec["incidents"]])
    summary = (f"{total} {'person' if total == 1 else 'people'} in {len(spec['groups'])} "
               f"{'group' if len(spec['groups']) == 1 else 'groups'}"
               + (f", {len(spec['incidents'])} incident{'s' if len(spec['incidents']) != 1 else ''}" if spec["incidents"] else "")
               + f", seed {spec['seed']}.")
    s = Story("custom", spec["name"], vt=["Custom"], mode="Custom · your scenario",
              tagline=summary + " Recorded from the real simulator; every passenger, assignment and incident below happened in the run.",
              requirements=["FR-1.4", "FR-1.5", "PERF-5", "PERF-9", "WEAR-3"]
              + sorted({{"MEDICAL": "EMR-3.14", "HELP": "EMR-3.1", "OBSTRUCTION": "FR-1.15",
                          "MAINTENANCE": "MNT-4.4", "FAULT": "MNT-4.6"}[i["type"]] for i in spec["incidents"]}),
              base=base, wear=WearConfig(balancing_enabled=policy != "SREMS_NO_WEAR"),
              traffic_minor=total > 12)
    s.sim.dispatch_policy = "NEAREST" if policy == "NEAREST" else "SREMS"
    for k, v in (sim_options or {}).items():   # dispatch tuning experiments (dispatch_benchmark)
        if k.startswith("wear."):
            s.sim.wear.config = dataclasses.replace(s.sim.wear.config, **{k[5:]: v})
        else:
            setattr(s.sim, k, v)
    for eid, c in spec["cars"].items():
        s.place(eid, c["floor"])
        s.preset_counters(eid, month=c["floors_month"], period=c["floors_month"] * 3, lifetime=c["floors_month"] * 20)
        s.flags["counters_start"][eid] = c["floors_month"]
        if c["state"] == "MAINTENANCE":
            s.sim.isolate(eid, "MAINTENANCE", technician_id="TECH-CARD-0007")
    s._avail = {eid: c.availability for eid, c in s.sim.cars.items()}

    tg = spec["targets"]
    waits = lambda s: [j["board"] - j["appear"] for j in s.journeys if j["board"] is not None]
    last: dict[str, tuple[int, str]] = {}

    def doors_safe(s: Story) -> bool:
        """A car that changed floor since the last second must have had its doors closed then."""
        ok = True
        for eid, car in s.sim.cars.items():
            if eid in last and last[eid][0] != car.current_floor and last[eid][1] != "CLOSED":
                ok = False
            last[eid] = (car.current_floor, car.door_state)
        return ok

    s.criterion("Every person reaches their destination; no request dropped", "PERF-5",
                lambda s: len(s.journeys) == total and not s.people, kind="final")
    s.criterion(f"Average wait ≤ {tg['avg_wait']} s (appearing at the landing to boarding)", "PERF-9",
                lambda s: bool(waits(s)) and mean(waits(s)) <= tg["avg_wait"], kind="final")
    s.criterion(f"95th-percentile wait ≤ {tg['p95_wait']} s", "PERF-9",
                lambda s: bool(waits(s)) and _pct(waits(s), .95) <= tg["p95_wait"], kind="final")
    s.criterion("E5 never takes an ordinary hall call", "FR-1.4",
                lambda s: all(d["chosen"] != "E5" for d in s.sim.decision_trace if d["call_type"] == "ORDINARY"), kind="always")
    s.criterion("No car leaves a floor with its doors open", "FR-1.15",
                doors_safe, kind="always")
    s.criterion("Active requests stay bounded (≤ 100)", "PERF-5",
                lambda s: s.sim.metrics()["active_requests"] <= 100, kind="always")

    groups_txt = "; ".join(
        f"{g['count']} {'with goods ' if g['kind'] == 'freight' else ''}from {_where(g['from'])} to {_where(g['to'])}"
        + (f" over {g['over']} s" if g["over"] else "") + (f" from t = {g['start']} s" if g["start"] else "")
        for g in spec["groups"])
    s.say(f"Your scenario: {groups_txt}. Watch which car Dispatch sends to each call.", pause=True)

    by_tick: dict[int, list[dict]] = {}
    for p in people:
        by_tick.setdefault(p["at"], []).append(p)
    inc_open: list[dict] = []
    pending = sorted(({**i, "phase": "start", "t": i["at"]} for i in spec["incidents"]), key=lambda x: x["t"])

    def spawn(tick: int) -> None:
        for p in by_tick.get(tick, []):
            person = s._new_person(p["from"], p["to"], None)
            person.kind = p["kind"]
            s._press(person, minor=s.traffic_minor)

    def incidents(tick: int) -> None:
        while pending and pending[0]["t"] <= tick:
            ev = pending.pop(0)
            _incident(s, ev, pending)
            if ev["phase"] == "start":
                inc_open.append(ev)

    tick = 0
    spawn(0)
    incidents(0)
    while tick < window:
        s.run(1)
        tick = s.sim.sim_tick
        spawn(tick)
        incidents(tick)
        if tick % 60 == 0 and s.journeys:
            w = waits(s)
            s._emit({"type": "Readings", "lane": "audit", "to": [],
                     "text": f"{tick // 60} min: {len(s.journeys)} of {total} delivered, average wait {mean(w):.1f} s, longest {max(w)} s."})
    # let everyone finish (bounded)
    for _ in range(LIMITS["drain"]):
        if len(s.journeys) == total and not s.people and not pending:
            break
        if pending and pending[0]["t"] <= s.sim.sim_tick:
            incidents(s.sim.sim_tick)
        s.run(1)
    k = kpis(s, total)
    s.say(("Everyone delivered. " if k["delivered"] == total else f"Stopped with {total - k['delivered']} people not delivered. ")
          + f"Average wait {k['avg_wait']} s, 95th percentile {k['p95_wait']} s, longest {k['max_wait']} s. "
          "The comparison below shows the same people under two other dispatch rules.", pause=False)
    s._emit({"type": "Readings", "lane": "audit", "to": [],
             "text": f"Done at t = {s.sim.sim_tick} s: {k['delivered']} of {total} delivered; average wait {k['avg_wait']} s, "
                     f"95th percentile {k['p95_wait']} s, average journey {k['avg_journey']} s."})
    s.run(2)
    return s, k


def _where(v: Any) -> str:
    if isinstance(v, list):
        return "floors " + ", ".join(floor_name(_floors(x, "x")[0]) for x in v[:6]) + ("…" if len(v) > 6 else "")
    if isinstance(v, int) or (isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip())):
        return f"floor {floor_name(int(v))}"
    return {"random": "random floors 2–30", "lower": "floors 2–15", "upper": "floors 16–30",
            "any": "any floor", "lobby": "the lobby", "b1": "B1"}.get(str(v).lower(), str(v))


def _incident(s: Story, ev: dict, pending: list[dict]) -> None:
    """Start or end one incident through the simulator's own commands."""
    car, typ = ev["car"], ev["type"]
    end = {**ev, "phase": "end", "t": ev["at"] + ev["duration"]}
    if ev["phase"] == "start":
        if typ in ("MEDICAL", "HELP"):
            s.say(f"Someone in {car} presses {typ}. Watch {car} drop its stops and its waiting calls move to other cars.", pause=True)
            res = s.act(f"Passenger in {car} presses {typ}.", lambda sim: sim.trigger_emergency(car, typ),
                        car=car, who=f"Passenger in {car}", code="medical" if typ == "MEDICAL" else "help",
                        lane="emergency", to=["dispatch", "audit"], expect_error=True)
            end["incident"] = (res or {}).get("incident_id") if isinstance(res, dict) else None
        elif typ == "OBSTRUCTION":
            s.say(f"Something blocks {car}'s door for {ev['duration']} s. The car must not move until it clears.", pause=True)
            s.act(f"Door sensor: obstruction on {car}.", lambda sim: sim.door_obstruction(car, True),
                  car=car, who="Door sensor", code="obstruction", expect_error=True)
        elif typ == "MAINTENANCE":
            s.say(f"A technician takes {car} out for {ev['duration']} s of maintenance. Its calls go to the other cars.", pause=True)
            s.act(f"Technician puts {car} in maintenance.",
                  lambda sim: sim.isolate(car, "MAINTENANCE", technician_id="TECH-CARD-0007"),
                  lane="maintenance", to=["dispatch", "audit"], who="Technician", car=car, code="isolate", expect_error=True)
        elif typ == "FAULT":
            s.say(f"{car}'s drive reports a blocking fault. It is isolated for {ev['duration']} s.", pause=True)
            res = s.act(f"{car} drive reports fault MOTOR_OVERTEMP.", lambda sim: sim.report_fault(car, "MOTOR_OVERTEMP"),
                        car=car, who=f"{car} drive", code="fault", expect_error=True)
            end["incident"] = (res or {}).get("incident_id") if isinstance(res, dict) else None
        pending.append(end)
        pending.sort(key=lambda x: x["t"])
        return
    # end of the incident
    if typ in ("MEDICAL", "HELP", "FAULT"):
        if typ == "FAULT":
            s.act(f"{car} drive: fault cleared.", lambda sim: sim.clear_fault(car), car=car, who=f"{car} drive",
                  code="fault", expect_error=True)
        iid = ev.get("incident")
        if iid:
            if typ != "FAULT":
                s.act(f"Admin acknowledges the {typ.lower()} incident.",
                      lambda sim: sim.incident_action(iid, "ACKNOWLEDGE", actor="ADMIN-CARD-0001"),
                      lane="emergency", to=["audit"], who="Admin", car=car, code="resolve", expect_error=True)
            s.act(f"{'Technician' if typ == 'FAULT' else 'Admin'} resolves the incident (recovery confirmed); {car} returns to service.",
                  lambda sim: sim.incident_action(iid, "RESOLVE", actor="TECH-CARD-0007" if typ == "FAULT" else "ADMIN-CARD-0001",
                                                  technician_confirmation=True),
                  lane="maintenance" if typ == "FAULT" else "emergency", to=["dispatch", "audit"],
                  who="Technician" if typ == "FAULT" else "Admin", car=car, code="resolve", expect_error=True)
    elif typ == "OBSTRUCTION":
        s.act(f"Door sensor: {car} clear again.", lambda sim: sim.door_obstruction(car, False),
              car=car, who="Door sensor", code="obstruction", expect_error=True)
    elif typ == "MAINTENANCE":
        s.act(f"Technician returns {car} to service (checks confirmed).",
              lambda sim: sim.isolate(car, "IN_SERVICE", technician_id="TECH-CARD-0007"),
              lane="maintenance", to=["dispatch", "audit"], who="Technician", car=car, code="return", expect_error=True)
    # passengers still inside press their floor again (blocked car calls are not replayed)
    for p in s.riders(car):
        if p.dest != s.sim.cars[car].current_floor:
            try:
                s.sim.submit_car_call(car, p.dest)
            except OperationalError:
                pass
    s._collect(minor_carcall=True)


def kpis(s: Story, total: int) -> dict:
    j = s.journeys
    w = [x["board"] - x["appear"] for x in j if x["board"] is not None]
    ride = [x["arrive"] - x["board"] for x in j if x["board"] is not None]
    trip = [x["arrive"] - x["appear"] for x in j]
    start = s.flags.get("counters_start") or {}
    floors = {eid: s.sim.wear.cars[eid].floors_month - start.get(eid, 0) for eid in CARS}
    res = [floors[e] for e in CARS[:4]]
    starts = {eid: s.sim.wear.cars[eid].starts_month - start.get(eid, 0) // 9 for eid in CARS}
    m = s.sim.metrics()
    month_now = [s.sim.wear.cars[e].floors_month for e in CARS[:4]]
    month_start = [start.get(e, 0) for e in CARS[:4]]
    by_floor: dict[int, list[int]] = {}
    for x in j:
        if x["board"] is not None:
            by_floor.setdefault(x["from"], []).append(x["board"] - x["appear"])
    hot = sorted(((f, round(mean(v), 1), len(v)) for f, v in by_floor.items() if len(v) >= 2), key=lambda r: -r[1])[:3]
    return {
        "month_gap_start": max(month_start) - min(month_start), "month_gap_end": max(month_now) - min(month_now),
        "slowest_floors": [{"floor": f, "avg_wait": a, "people": n} for f, a, n in hot],
        "people": total, "delivered": len(j),
        "avg_wait": _round(mean(w)) if w else None, "p95_wait": _pct(w, .95), "max_wait": max(w) if w else None,
        "over_60": sum(1 for x in w if x > 60), "avg_ride": _round(mean(ride)) if ride else None,
        "avg_journey": _round(mean(trip)) if trip else None, "p95_journey": _pct(trip, .95),
        "finished_at": s.sim.sim_tick, "floors": floors, "floors_total": sum(floors.values()),
        "stops": sum(starts.values()), "floors_spread": max(res) - min(res),
        "hall_avg_wait": _round(m["mean_wait_seconds"]), "hall_p95_wait": m["p95_wait_seconds"],
    }


def compare(spec_in: dict) -> dict:
    """Run the scenario under all three dispatch rules; return the SREMS recording with the
    comparison attached."""
    spec = normalise(spec_in)
    results = {}
    story_obj = None
    for key, label, note in POLICIES:
        s, k = run(spec, key)
        results[key] = {"key": key, "label": label, "note": note, "kpis": k}
        if key == "SREMS":
            story_obj = s
    story = story_obj.finish(strict=False)
    story["key"] = "custom-" + re.sub(r"[^a-z0-9]+", "-", spec["name"].lower()).strip("-")[:40]
    story["custom"] = True
    story["spec"] = {k: v for k, v in spec.items() if k != "groups"} | {
        "groups": [{k: v for k, v in g.items() if not k.startswith("_")} for g in spec["groups"]]}
    story["comparison"] = {"policies": [results[k] for k, _, _ in POLICIES], "verdict": verdict(results, spec)}
    story["recordedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return story


def _gap(closed: int) -> str:
    return (f"narrowed by {closed} floors" if closed > 0 else f"widened by {-closed} floors" if closed < 0
            else "stayed the same")


def verdict(r: dict, spec: dict) -> list[str]:
    a, b, n = r["SREMS"]["kpis"], r["SREMS_NO_WEAR"]["kpis"], r["NEAREST"]["kpis"]
    out = []
    def pct(x, y):
        return None if not y else round(100 * (y - x) / y)
    if a["avg_wait"] is not None and n["avg_wait"] is not None:
        d = pct(a["avg_wait"], n["avg_wait"])
        if d is not None and d >= 3:
            out.append(f"SREMS dispatch cut the average wait by {d} % against the nearest-car rule ({a['avg_wait']} s vs {n['avg_wait']} s).")
        elif d is not None and d <= -3:
            out.append(f"In this scenario the nearest-car rule waited less on average ({n['avg_wait']} s vs {a['avg_wait']} s). Worth investigating: look at the long waits in the log.")
        else:
            out.append(f"Average wait is about the same as the nearest-car rule ({a['avg_wait']} s vs {n['avg_wait']} s): the traffic is light enough that any rule works.")
    if a["p95_wait"] is not None and n["p95_wait"] is not None and a["p95_wait"] != n["p95_wait"]:
        better = a["p95_wait"] < n["p95_wait"]
        out.append(f"The slowest 5 % of passengers waited {'less' if better else 'more'}: {a['p95_wait']} s vs {n['p95_wait']} s.")
    if a["avg_wait"] is not None and b["avg_wait"] is not None:
        cost = round(a["avg_wait"] - b["avg_wait"], 1)
        closed_a = a["month_gap_start"] - a["month_gap_end"]
        closed_b = b["month_gap_start"] - b["month_gap_end"]
        if a["month_gap_start"] == 0 and cost == 0:
            out.append("The trip counters of E1–E4 started level, so wear balancing had nothing to do here.")
        else:
            out.append(
                (f"Wear balancing cost {cost} s of average wait" if cost > 0 else
                 f"Wear balancing saved {abs(cost)} s of average wait" if cost < 0 else "Wear balancing cost no waiting time")
                + f"; the gap between the most- and least-used of E1–E4 this month {_gap(closed_a)}"
                + f" (without balancing it {_gap(closed_b)}).")
    if a["slowest_floors"]:
        out.append("Longest average waits by starting floor: " + ", ".join(
            f"{floor_name(r['floor'])} ({r['avg_wait']} s, {r['people']} {'person' if r['people'] == 1 else 'people'})"
            for r in a["slowest_floors"]) + ". If these keep coming up, parking an idle car nearer to them would help most.")
    tg = spec["targets"]
    if a["avg_wait"] is not None:
        ok = a["avg_wait"] <= tg["avg_wait"] and (a["p95_wait"] or 0) <= tg["p95_wait"]
        out.append(("Meets" if ok else "Misses") + f" the targets you set (average ≤ {tg['avg_wait']} s, 95th percentile ≤ {tg['p95_wait']} s).")
    if a["delivered"] < a["people"]:
        out.append(f"{a['people'] - a['delivered']} people were not delivered within the run: the fleet could not keep up or a car stayed out of service.")
    return out


EXAMPLE = {
    "name": "Monday 08:00 rush with a fault",
    "start": "07:55", "seed": 7,
    "cars": {"E1": {"floor": 1}, "E2": {"floor": 8, "floors_month": 456000}, "E3": {"floor": 16},
             "E4": {"floor": 24}, "E5": {"floor": 1}},
    "groups": [
        {"label": "Residents leaving", "count": 40, "from": "random", "to": "lobby", "start": 0, "over": 180},
        {"label": "Drivers to parking", "count": 10, "from": "random", "to": "B1", "start": 0, "over": 180},
        {"label": "Returning residents", "count": 8, "from": "lobby", "to": "upper", "start": 30, "over": 120},
        {"label": "Delivery", "count": 2, "from": 1, "to": [12, 22], "start": 20, "over": 30, "kind": "freight"},
    ],
    "incidents": [{"type": "FAULT", "car": "E3", "at": 60, "duration": 45}],
    "targets": {"avg_wait": 60, "p95_wait": 120},
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?", help="scenario JSON file")
    ap.add_argument("--example", action="store_true", help="print an example scenario and exit")
    ap.add_argument("--player", action="store_true", help="add the recording to frontend/public/player/custom.js")
    ap.add_argument("--out", default="frontend/public/player/custom.js")
    a = ap.parse_args(argv)
    if a.example:
        print(json.dumps(EXAMPLE, indent=2))
        return 0
    if not a.spec:
        ap.error("give a scenario file, or --example")
    try:
        story = compare(json.loads(Path(a.spec).read_text(encoding="utf-8")))
    except (SpecError, json.JSONDecodeError) as exc:
        print(f"Scenario not valid: {exc}", file=sys.stderr)
        return 2
    print(f"{story['title']}: {story['tagline'].split(' Recorded')[0]}")
    print(f"{'Dispatch rule':34s} {'avg wait':>9s} {'p95':>6s} {'max':>6s} {'journey':>8s} {'floors':>7s} {'delivered':>10s}")
    for p in story["comparison"]["policies"]:
        k = p["kpis"]
        print(f"{p['label']:34s} {k['avg_wait'] or '-':>8}s {k['p95_wait'] or '-':>5}s {k['max_wait'] or '-':>5}s "
              f"{k['avg_journey'] or '-':>7}s {k['floors_total']:>7} {k['delivered']:>4}/{k['people']}")
    for line in story["comparison"]["verdict"]:
        print("- " + line)
    for c in story["criteria"]:
        print(("PASS " if c["passed_at"] is not None else "FAIL ") + c["text"])
    if a.player:
        out = Path(a.out)
        existing = []
        if out.exists():
            text = out.read_text(encoding="utf-8")
            m = re.search(r"window\.SREMS_CUSTOM = (\[.*\]);", text, re.S)
            existing = json.loads(m[1]) if m else []
        existing = [x for x in existing if x.get("key") != story["key"]] + [story]
        out.write_text("// Generated by `python -m simulation.custom_scenario --player`. Your own scenarios.\n"
                       "window.SREMS_CUSTOM = " + json.dumps(existing, separators=(",", ":"), default=str) + ";\n",
                       encoding="utf-8")
        print(f"added to {out} ({len(existing)} custom scenario{'s' if len(existing) != 1 else ''}); open the player and pick it under 'Your tests'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
