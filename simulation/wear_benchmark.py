"""Long-run (1 month / 6 months) wear-and-tear benchmark with full passenger journeys.

Why a new benchmark
-------------------
``cibse_benchmark.py`` and ``traffic_profiles.py`` only submit hall calls: no passenger ever
selects a destination, so cars only travel to pick-up floors. That is enough for short
wait-time checks but it cannot say anything about distance travelled, which is what the
instructor's wear-and-tear requirement (WEAR-2, WEAR-3) is about.

This module drives ``OperationalSimulator`` with complete journeys:

1. a passenger arrives at an origin floor and presses UP/DOWN (hall call, FR-1.3);
2. when the assigned car arrives, the passenger boards and selects a destination
   (car call, FR-1.9; authorization is assumed granted, this is a test stub);
3. the car carries the passenger, so floors travelled include the loaded run.

Traffic model (residential, CIBSE Guide D style; SRS CAP-1 population)
---------------------------------------------------------------------
* 600 apartments x 2.5 occupants = 1,500 residents, about 3.9 journeys per person per day.
* Hourly demand with a morning down-peak (07-09), an evening up-peak (17-20) and a quiet
  night (22-05); weekends have a smaller morning peak and a busier midday.
* Down-peak: 70 % to the lobby/B1, 20 % up from lobby/B1, 10 % inter-floor. Up-peak: the
  reverse. Other hours: 45 / 45 / 10. 25 % of lobby trips use B1 (parking).
* 3 % of journeys are freight/goods/pet trips on E5 (never to or from B1, FR-1.13).
* Optional maintenance outages take residential cars out of service for whole days.

Everything is seeded (BEN-2): the same arguments always give the same numbers.

Run from the repository root::

    python -m simulation.wear_benchmark --months 1
    python -m simulation.wear_benchmark --months 6 --compare --outages --out docs/wear_report.json
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import random
import time
from typing import Callable

from simulation.operational import OperationalError, OperationalSimulator
from simulation.wear import WearConfig, add_months, month_floor, spread_pct

# Journeys per hour for the whole building (weekday), index = hour of day.
WEEKDAY_HOURLY = [20, 15, 10, 10, 15, 60, 160, 650, 650, 220, 180, 200,
                  260, 260, 190, 190, 230, 600, 600, 600, 260, 240, 90, 50]
WEEKEND_HOURLY = [30, 20, 12, 10, 10, 30, 80, 260, 300, 300, 320, 340,
                  360, 340, 300, 300, 320, 420, 420, 380, 300, 260, 120, 60]
FREIGHT_SHARE = 0.03
B1_SHARE_OF_LOBBY = 0.25


@dataclass(frozen=True)
class Outage:
    """A residential car taken out of service (maintenance) for a time window."""

    elevator_id: str
    start: datetime
    hours: float


@dataclass
class WearBenchmarkConfig:
    months: int = 1
    start: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    days: int | None = None  # short runs (tests); overrides ``months`` when set
    seed: int = 2030
    demand_scale: float = 1.0
    balancing_enabled: bool = True
    outages: list[Outage] = field(default_factory=list)
    wear_overrides: dict = field(default_factory=dict)


@dataclass
class WearBenchmarkResult:
    label: str
    balancing_enabled: bool
    months: int
    seed: int
    start: str
    end: str
    journeys: int
    hall_calls_completed: int
    wall_seconds: float
    month_windows: list[dict]
    period_windows: list[dict]
    per_car_floors_total: dict[str, int]
    per_car_starts_total: dict[str, int]
    per_car_doors_total: dict[str, int]
    wait: dict[str, dict]
    outages: list[dict]
    verdict: dict

    def to_dict(self) -> dict:
        return asdict(self)


def default_outages(start: datetime, months: int, seed: int) -> list[Outage]:
    """One planned 8-hour maintenance visit per residential car per month, plus one
    unplanned 2-day outage of a random car in month 1 (stress for the catch-up logic)."""
    rng = random.Random(seed + 77)
    outages: list[Outage] = []
    for m in range(months):
        m_start = add_months(month_floor(start), m)
        days = (add_months(m_start, 1) - m_start).days
        for eid in ("E1", "E2", "E3", "E4"):
            day = rng.randint(1, days - 3)
            outages.append(Outage(eid, m_start + timedelta(days=day, hours=9), 8.0))
    victim = rng.choice(("E1", "E2", "E3", "E4"))
    outages.append(Outage(victim, month_floor(start) + timedelta(days=10, hours=6), 48.0))
    return sorted(outages, key=lambda o: o.start)


class _Journey:
    __slots__ = ("destination", "accepted_tick")

    def __init__(self, destination: int, accepted_tick: int) -> None:
        self.destination = destination
        self.accepted_tick = accepted_tick


def _pick_journey(rng: random.Random, hour: int) -> tuple[int, int, bool]:
    """Return (origin, destination, is_freight)."""
    if rng.random() < FREIGHT_SHARE:
        origin = rng.randint(1, 30)
        dest = rng.randint(1, 30)
        while dest == origin:
            dest = rng.randint(1, 30)
        return origin, dest, True
    if 7 <= hour < 10:
        down, up = 0.70, 0.20
    elif 17 <= hour < 21:
        down, up = 0.20, 0.70
    else:
        down, up = 0.45, 0.45
    lobby = -1 if rng.random() < B1_SHARE_OF_LOBBY else 1
    roll = rng.random()
    if roll < down:
        return rng.randint(2, 30), lobby, False
    if roll < down + up:
        return lobby, rng.randint(2, 30), False
    origin = rng.randint(2, 30)
    dest = rng.randint(2, 30)
    while dest == origin:
        dest = rng.randint(2, 30)
    return origin, dest, False


def _bench_profile(ts: datetime) -> str:
    """PERF-8 windows on the benchmark clock (its timestamps are building-local time),
    independent of SREMS_BUILDING_UTC_OFFSET_HOURS used by the live server."""
    hour = ts.hour
    if 7 <= hour < 9 or 17 <= hour < 20:
        return "PEAK"
    if hour >= 22 or hour < 5:
        return "OFF_PEAK"
    return "NORMAL"


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    return sorted_vals[max(0, math.ceil(p * len(sorted_vals)) - 1)]


def run_wear_benchmark(
    cfg: WearBenchmarkConfig,
    *,
    label: str = "run",
    progress: Callable[[float, str], None] | None = None,
    sim: OperationalSimulator | None = None,
) -> WearBenchmarkResult:
    """Run the benchmark. ``sim`` lets a caller warm up its own (non-DB) simulator."""
    wear_cfg = WearConfig(**({"balancing_enabled": cfg.balancing_enabled} | cfg.wear_overrides))
    if sim is None:
        sim = OperationalSimulator(auto_advance=False, load_from_db=False, wear_config=wear_cfg)
    start = cfg.start.astimezone(timezone.utc)
    sim.reset(base_time=start)
    end = (start + timedelta(days=cfg.days)) if cfg.days else add_months(month_floor(start), cfg.months)
    total_ticks = int((end - start).total_seconds())
    rng = random.Random(cfg.seed)

    waits: dict[str, list[float]] = {"PEAK": [], "OFF_PEAK": [], "NORMAL": [], "ALL": []}
    waiting: dict[str, list[_Journey]] = {}
    journeys = 0
    completed = 0
    outages = sorted(cfg.outages, key=lambda o: o.start)
    pending_out = [(int((o.start - start).total_seconds()), o) for o in outages]
    pending_back: list[tuple[int, str]] = []
    outage_log: list[dict] = []
    wall0 = time.perf_counter()

    def rate_at(tick: int) -> float:
        ts = start + timedelta(seconds=tick)
        table = WEEKEND_HOURLY if ts.weekday() >= 5 else WEEKDAY_HOURLY
        return table[ts.hour] * cfg.demand_scale / 3600.0

    def harvest(requests) -> None:
        nonlocal completed
        for req in requests:
            if req.wait_seconds is None or req.call_type != "ORDINARY":
                continue
            completed += 1
            waits["ALL"].append(req.wait_seconds)
            waits[_bench_profile(req.accepted_at)].append(req.wait_seconds)

    # Poisson arrivals via thinning: the next arrival is drawn at the maximum rate and
    # accepted with probability rate(t)/max_rate (exact for a piecewise-constant rate).
    max_rate = max(max(WEEKDAY_HOURLY), max(WEEKEND_HOURLY)) * cfg.demand_scale / 3600.0
    next_arrival = rng.expovariate(max_rate)
    tick = 0
    last_compact = 0
    last_report = -1.0
    while tick < total_ticks:
        # Scheduled maintenance outages (labelled test-stub equipment events).
        while pending_out and pending_out[0][0] <= tick:
            _, outage = pending_out.pop(0)
            car = sim.cars[outage.elevator_id]
            if car.availability == "IN_SERVICE":
                sim.isolate(outage.elevator_id, "MAINTENANCE", technician_id="BENCH-TECH")
                back = tick + int(outage.hours * 3600)
                pending_back.append((back, outage.elevator_id))
                pending_back.sort()
                outage_log.append({"elevator_id": outage.elevator_id,
                                   "start": (start + timedelta(seconds=tick)).isoformat(),
                                   "hours": outage.hours})
        while pending_back and pending_back[0][0] <= tick:
            _, eid = pending_back.pop(0)
            try:
                sim.isolate(eid, "IN_SERVICE", technician_id="BENCH-TECH")
            except OperationalError:
                pending_back.append((tick + 60, eid))
                pending_back.sort()

        # New passengers arriving during this second.
        while next_arrival < tick + 1:
            arrival_tick = int(next_arrival)
            next_arrival += rng.expovariate(max_rate)
            if rng.random() * max_rate > rate_at(arrival_tick):
                continue
            hour = (start + timedelta(seconds=arrival_tick)).hour
            origin, dest, freight = _pick_journey(rng, hour)
            try:
                if freight:
                    req, _ = sim.submit_hall_call(origin_floor=origin, call_type="FREIGHT")
                else:
                    direction = "UP" if (dest if dest != -1 else 0) > (origin if origin != -1 else 0) else "DOWN"
                    req, _ = sim.submit_hall_call(origin_floor=origin, direction=direction)
            except OperationalError:
                continue  # e.g. freight call while E5 is OCCUPIED: rejected, not queued (FR-1.13)
            journeys += 1
            waiting.setdefault(req["request_id"], []).append(_Journey(dest, tick))

        # Fast-forward through idle night time (nothing moving, nobody waiting).
        idle = (not waiting and all(not c.stops and c.dwell_remaining == 0 for c in sim.cars.values()))
        if idle:
            gap = int(next_arrival) - tick
            if pending_out:
                gap = min(gap, pending_out[0][0] - tick)
            if pending_back:
                gap = min(gap, pending_back[0][0] - tick)
            gap = min(gap, total_ticks - tick)
            if gap > 60:
                sim.step_from_clock(gap)
                tick += gap
                continue
        sim.advance(1)
        tick += 1

        # Passengers board the car that arrived and press their destination.
        if waiting:
            for rid in list(waiting):
                req = sim.hall_calls.get(rid)
                if req is None or req.status != "COMPLETED":
                    continue
                car_id = req.assigned_car
                for journey in waiting.pop(rid):
                    if car_id is None or journey.destination == sim.cars[car_id].current_floor:
                        continue
                    try:
                        sim.submit_car_call(car_id, journey.destination)
                    except OperationalError:
                        pass  # car became unavailable while the doors were open

        if tick - last_compact >= 6 * 3600:
            harvest(sim.compact_history(keep_seconds=1800))
            last_compact = tick
        if progress is not None:
            frac = tick / total_ticks
            if frac - last_report >= 0.01:
                last_report = frac
                progress(frac, f"{label}: simulated {(start + timedelta(seconds=tick)).date()}")

    # Drain, then close the last month window exactly at the end boundary.
    for _ in range(600):
        if not any(c.stops for c in sim.cars.values()):
            break
        sim.advance(1)
    sim.wear.advance_to(end)
    harvest(sim.compact_history(keep_seconds=0))
    harvest(r for r in sim.hall_calls.values() if r.status == "COMPLETED")

    wait_summary: dict[str, dict] = {}
    for key, vals in waits.items():
        vals.sort()
        wait_summary[key] = {
            "count": len(vals),
            "mean_seconds": round(sum(vals) / len(vals), 2) if vals else None,
            "p95_seconds": _percentile(vals, 0.95),
        }

    def _iso_rows(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            r = dict(r)
            for k in ("window_start", "window_end", "closed_at"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k].isoformat()
            out.append(r)
        return out

    months = _iso_rows(sim.wear.month_history)
    periods = _iso_rows(sim.wear.period_history)
    res_months = [m for m in months if m["wear_group"] == "RESIDENTIAL"]
    res_periods = [p for p in periods if p["wear_group"] == "RESIDENTIAL"]
    lifetime = {eid: c.floors_lifetime for eid, c in sim.wear.cars.items()}
    peak = wait_summary["PEAK"]
    off = wait_summary["OFF_PEAK"]
    verdict = {
        "monthly_max_spread_pct": max((m["spread_pct"] for m in res_months), default=0.0),
        "monthly_all_within_2pct": all(m["within_limit"] for m in res_months),
        "period_spreads_pct": [p["spread_pct"] for p in res_periods],
        "period_all_within_5pct": all(p["within_limit"] for p in res_periods) if res_periods else None,
        "cumulative_spread_pct": round(spread_pct(lifetime[e] for e in ("E1", "E2", "E3", "E4")), 4),
        "month_to_date_spread_pct": round(
            spread_pct(sim.wear.cars[e].floors_month for e in ("E1", "E2", "E3", "E4")), 4),
        "perf9_peak_mean_ok": peak["mean_seconds"] is not None and peak["mean_seconds"] <= 60,
        "perf9_peak_p95_ok": peak["p95_seconds"] is not None and peak["p95_seconds"] <= 120,
        "perf10_offpeak_mean_ok": off["mean_seconds"] is not None and off["mean_seconds"] <= 90,
    }
    return WearBenchmarkResult(
        label=label,
        balancing_enabled=cfg.balancing_enabled,
        months=cfg.months,
        seed=cfg.seed,
        start=start.isoformat(),
        end=end.isoformat(),
        journeys=journeys,
        hall_calls_completed=completed,
        wall_seconds=round(time.perf_counter() - wall0, 2),
        month_windows=months,
        period_windows=periods,
        per_car_floors_total=lifetime,
        per_car_starts_total={eid: c.starts_lifetime for eid, c in sim.wear.cars.items()},
        per_car_doors_total={eid: c.doors_lifetime for eid, c in sim.wear.cars.items()},
        wait=wait_summary,
        outages=outage_log,
        verdict=verdict,
    )


def _print(result: WearBenchmarkResult) -> None:
    print(f"\n=== {result.label} (balancing={'ON' if result.balancing_enabled else 'OFF'}, "
          f"{result.months} month(s), seed {result.seed}, {result.wall_seconds}s wall) ===")
    print(f"journeys {result.journeys:,}  completed hall calls {result.hall_calls_completed:,}")
    for m in result.month_windows:
        if m["wear_group"] != "RESIDENTIAL":
            continue
        cars = "  ".join(f"{k}={v:,}" for k, v in m["per_car_floors"].items())
        flag = "OK " if m["within_limit"] else "FAIL"
        print(f"  month {m['window_start'][:7]}  {cars}  spread {m['spread_pct']:.3f}% [{flag}]")
    for p in result.period_windows:
        if p["wear_group"] == "RESIDENTIAL":
            print(f"  6-month period from {p['window_start'][:7]}: spread {p['spread_pct']:.3f}% "
                  f"[{'OK' if p['within_limit'] else 'FAIL'}]")
    w = result.wait
    print(f"  wait PEAK mean {w['PEAK']['mean_seconds']}s p95 {w['PEAK']['p95_seconds']}s | "
          f"OFF_PEAK mean {w['OFF_PEAK']['mean_seconds']}s | ALL mean {w['ALL']['mean_seconds']}s")
    print(f"  verdict {result.verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=1)
    parser.add_argument("--seed", type=int, default=2030)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--demand-scale", type=float, default=1.0)
    parser.add_argument("--outages", action="store_true", help="inject maintenance outages")
    parser.add_argument("--compare", action="store_true", help="also run with balancing OFF")
    parser.add_argument("--out", default=None, help="write JSON results to this path")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    outages = default_outages(start, args.months, args.seed) if args.outages else []
    runs = [True, False] if args.compare else [True]
    results = []
    for enabled in runs:
        cfg = WearBenchmarkConfig(months=args.months, start=start, seed=args.seed,
                                  demand_scale=args.demand_scale, balancing_enabled=enabled,
                                  outages=outages)
        res = run_wear_benchmark(cfg, label="wear-balancing ON" if enabled else "baseline (balancing OFF)")
        _print(res)
        results.append(res.to_dict())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"generated_by": "python -m simulation.wear_benchmark", "args": vars(args),
                       "results": results}, fh, indent=2, default=str)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
