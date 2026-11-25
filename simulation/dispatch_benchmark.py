"""Dispatch tuning benchmark: is the SREMS dispatch as good as it can be made here?

Runs a fixed set of scenarios (six traffic patterns x several seeds, plus "one car is behind on
its trip counter" variants) under several dispatch variants, on exactly the same people, and
reports passenger figures. A change to the dispatch is kept only if it helps across the set
without breaking the instructor's wear requirement, which ``simulation.wear_benchmark`` checks
over months.

Run from the repository root::

    python -m simulation.dispatch_benchmark              # all variants, table
    python -m simulation.dispatch_benchmark --seeds 10 --out docs/dispatch_report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
import time

from simulation import custom_scenario as cs

# Traffic patterns (same as the player's "New test" presets).
PATTERNS = {
    "morning": ("07:45", [
        {"count": 45, "from": "random", "to": "lobby", "start": 0, "over": 240},
        {"count": 15, "from": "random", "to": "B1", "start": 0, "over": 240},
        {"count": 6, "from": "lobby", "to": "random", "start": 0, "over": 240}]),
    "evening": ("18:00", [
        {"count": 45, "from": "lobby", "to": "random", "start": 0, "over": 240},
        {"count": 12, "from": "B1", "to": "random", "start": 0, "over": 240},
        {"count": 8, "from": "random", "to": "lobby", "start": 0, "over": 240}]),
    "lunch": ("12:00", [
        {"count": 25, "from": "random", "to": "lobby", "start": 0, "over": 300},
        {"count": 25, "from": "lobby", "to": "random", "start": 60, "over": 300}]),
    "interfloor": ("15:00", [{"count": 30, "from": "random", "to": "random", "start": 0, "over": 300}]),
    "movein": ("10:00", [
        {"count": 20, "from": "random", "to": "lobby", "start": 0, "over": 300},
        {"count": 10, "from": "lobby", "to": "random", "start": 0, "over": 300},
        {"count": 8, "from": "lobby", "to": [12, 18, 24], "start": 0, "over": 300, "kind": "freight"}]),
    "stress": ("08:00", [
        {"count": 150, "from": "random", "to": "lobby", "start": 0, "over": 180},
        {"count": 40, "from": "lobby", "to": "random", "start": 0, "over": 180}]),
    "light": ("22:00", [{"count": 12, "from": "any", "to": "any", "start": 0, "over": 600}]),
}

# Dispatch variants: simulator attributes set before the run (None = as currently built).
VARIANTS = {
    "current (busy gate, 15 s cap)": {},
    "previous (no gate, 30 s cap)": {"wear_busy_calls_per_car": None, "wear.max_penalty_seconds": 30.0},
    "busy gate, 30 s cap": {"wear.max_penalty_seconds": 30.0},
    "busy gate, 20 s cap": {"wear.max_penalty_seconds": 20.0},
    "busy gate, 10 s cap": {"wear.max_penalty_seconds": 10.0},
    "gate at 0.5 calls/car": {"wear_busy_calls_per_car": 0.5},
    "gate at 2 calls/car": {"wear_busy_calls_per_car": 2.0},
    "current + overload risk": {"overload_risk_seconds": 30.0},
    "no wear penalty (reference)": {"_policy": "SREMS_NO_WEAR"},
    "nearest car (reference)": {"_policy": "NEAREST"},
}


def scenarios(seeds: int) -> list[tuple[str, dict]]:
    out = []
    for name, (start, groups) in PATTERNS.items():
        for seed in range(1, seeds + 1):
            out.append((f"{name}", {"name": name, "start": start, "seed": seed, "groups": groups}))
            if name in ("morning", "stress", "light", "lunch"):
                # one car 9,000 floors behind this month (e.g. back from two days of maintenance)
                out.append((f"{name}+lag", {"name": f"{name} lag", "start": start, "seed": seed, "groups": groups,
                                            "cars": {"E2": {"floor": 8, "floors_month": 456_000}}}))
    return out


def run_variant(opts: dict, scen: list[tuple[str, dict]]) -> dict:
    opts = dict(opts)
    policy = opts.pop("_policy", "SREMS")
    rows = []
    for tag, spec in scen:
        _, k = cs.run(cs.normalise(spec), policy, sim_options=opts)
        rows.append({"scenario": tag, **{x: k[x] for x in ("people", "delivered", "avg_wait", "p95_wait", "max_wait",
                                                         "over_60", "avg_journey", "floors_total",
                                                         "month_gap_start", "month_gap_end")}})
    lag = [r for r in rows if r["scenario"].endswith("+lag")]
    return {
        "rows": rows,
        "mean_avg_wait": round(mean(r["avg_wait"] for r in rows), 2),
        "mean_p95_wait": round(mean(r["p95_wait"] for r in rows), 2),
        "worst_p95_wait": max(r["p95_wait"] for r in rows),
        "people_over_60": sum(r["over_60"] for r in rows),
        "mean_journey": round(mean(r["avg_journey"] for r in rows), 2),
        "undelivered": sum(r["people"] - r["delivered"] for r in rows),
        "floors": sum(r["floors_total"] for r in rows),
        "lag_gap_closed": sum(r["month_gap_start"] - r["month_gap_end"] for r in lag),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--only", nargs="*", help="variant names to run")
    ap.add_argument("--out")
    a = ap.parse_args()
    scen = scenarios(a.seeds)
    print(f"{len(scen)} scenario runs per variant ({len(PATTERNS)} patterns x {a.seeds} seeds, plus lagging-car variants)")
    print(f"{'variant':30s} {'avg wait':>9s} {'p95':>7s} {'worst p95':>10s} {'>60 s':>7s} {'journey':>8s} {'floors':>8s} {'undeliv':>8s} {'lag gap closed':>15s}")
    report = {}
    for name, opts in VARIANTS.items():
        if a.only and name not in a.only:
            continue
        t0 = time.perf_counter()
        r = run_variant(opts, scen)
        report[name] = {"options": opts, **r}
        print(f"{name:30s} {r['mean_avg_wait']:>8.2f}s {r['mean_p95_wait']:>6.1f}s {r['worst_p95_wait']:>9}s "
              f"{r['people_over_60']:>7} {r['mean_journey']:>7.1f}s {r['floors']:>8} {r['undelivered']:>8} "
              f"{r['lag_gap_closed']:>15}   ({time.perf_counter() - t0:.0f} s)")
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
