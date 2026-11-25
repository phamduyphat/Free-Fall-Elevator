"""Wear-and-tear balancing (instructor NFR, phase 2) — WEAR-1..WEAR-6, VT-33..VT-36.

Database-free (unittest compatible). Run: ``python -m unittest tests.test_wear_leveling``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from simulation.operational import OperationalSimulator, Stop
from simulation.wear import WearConfig, WearTracker, add_months, month_floor, spread_pct
from simulation.wear_benchmark import WearBenchmarkConfig, run_wear_benchmark

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestWearTracker(unittest.TestCase):
    def test_spread_definition_is_max_minus_min_over_max(self):
        self.assertAlmostEqual(spread_pct([100, 98, 99, 100]), 2.0)
        self.assertEqual(spread_pct([0, 0, 0]), 0.0)
        self.assertEqual(spread_pct([]), 0.0)

    def test_calendar_helpers(self):
        self.assertEqual(month_floor(datetime(2026, 2, 17, 13, 5, tzinfo=timezone.utc)),
                         datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(add_months(datetime(2026, 11, 1, tzinfo=timezone.utc), 3),
                         datetime(2027, 2, 1, tzinfo=timezone.utc))

    def test_month_window_closes_and_monthly_counter_resets(self):
        """WEAR-2/WEAR-4: the month is judged on its own distances, then the month counter resets."""
        tracker = WearTracker(["E1", "E2", "E3", "E4", "E5"], JAN + timedelta(days=3), WearConfig())
        for eid, floors in {"E1": 1000, "E2": 990, "E3": 1000, "E4": 1000, "E5": 300}.items():
            tracker.record_move(eid, floors)
        self.assertEqual(tracker.advance_to(JAN + timedelta(days=20)), [])
        closed = tracker.advance_to(datetime(2026, 2, 1, tzinfo=timezone.utc))
        res = [r for r in closed if r["wear_group"] == "RESIDENTIAL"][0]
        self.assertEqual(res["window_type"], "MONTH")
        self.assertAlmostEqual(res["spread_pct"], 1.0)
        self.assertTrue(res["within_limit"])
        frt = [r for r in closed if r["wear_group"] == "FREIGHT"][0]
        self.assertTrue(frt["within_limit"])  # single-car group
        self.assertEqual(tracker.cars["E1"].floors_month, 0)
        self.assertEqual(tracker.cars["E1"].floors_period, 1000)
        self.assertEqual(tracker.cars["E1"].floors_lifetime, 1000)

    def test_month_limit_is_strict_and_period_limit_inclusive(self):
        tracker = WearTracker(["E1", "E2", "E3", "E4"], JAN, WearConfig())
        tracker.record_move("E1", 100)
        tracker.record_move("E2", 98)  # exactly 2 % -> not "under 2 %"
        tracker.record_move("E3", 100)
        tracker.record_move("E4", 100)
        rows = tracker.advance_to(datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertFalse(rows[0]["within_limit"])

    def test_trip_counter_resets_every_six_months(self):
        """WEAR-5: the 6-month (period) counter is judged at <= 5 % and then reset."""
        tracker = WearTracker(["E1", "E2", "E3", "E4"], JAN, WearConfig())
        for month in range(6):
            tracker.record_move("E1", 1000)
            tracker.record_move("E2", 1000)
            tracker.record_move("E3", 990)
            tracker.record_move("E4", 1000)
            tracker.advance_to(add_months(JAN, month + 1) - timedelta(seconds=1))
            self.assertEqual(tracker.period_history, [])
            tracker.advance_to(add_months(JAN, month + 1))
        self.assertEqual(len(tracker.month_history), 6)
        self.assertEqual(len(tracker.period_history), 1)
        period = tracker.period_history[0]
        self.assertEqual(period["per_car_floors"]["E3"], 5940)
        self.assertAlmostEqual(period["spread_pct"], 1.0)
        self.assertTrue(period["within_limit"])
        self.assertEqual(tracker.cars["E1"].floors_period, 0)  # reset
        self.assertEqual(tracker.cars["E1"].floors_lifetime, 6000)  # lifetime kept
        self.assertEqual(tracker.period_start, datetime(2026, 7, 1, tzinfo=timezone.utc))

    def test_usage_penalty_is_proportional_and_capped(self):
        cfg = WearConfig(gain_seconds=60, max_penalty_seconds=30, reference_floors=1000)
        tracker = WearTracker(["E1", "E2", "E3", "E4"], JAN, cfg)
        tracker.record_move("E1", 10_000)
        tracker.record_move("E2", 10_100)  # 100 floors ahead -> 6 s
        tracker.record_move("E3", 10_050)  # 50 floors ahead, normalised by the 50,000 reference -> 3 s
        tracker.record_move("E4", 50_000)  # far ahead -> capped
        pen = tracker.usage_penalties(["E1", "E2", "E3", "E4"])
        self.assertEqual(pen["E1"], 0.0)
        self.assertAlmostEqual(pen["E3"], 60 * 50 / (0.02 * 50_000), places=6)
        self.assertEqual(pen["E4"], 30.0)
        # Only eligible candidates count: without E4 the reference shrinks.
        pen2 = tracker.usage_penalties(["E1", "E3"])
        self.assertAlmostEqual(pen2["E3"], min(30.0, 60 * 50 / (0.02 * 10_050)), places=6)
        disabled = WearTracker(["E1", "E2"], JAN, WearConfig(balancing_enabled=False))
        disabled.record_move("E2", 999)
        self.assertEqual(disabled.usage_penalties(["E1", "E2"]), {"E1": 0.0, "E2": 0.0})


class TestWearInSimulator(unittest.TestCase):
    def setUp(self):
        self.sim = OperationalSimulator(auto_advance=False, load_from_db=False, wear_config=WearConfig())
        self.sim.reset(base_time=JAN + timedelta(hours=10))

    def test_counters_follow_every_floor_start_and_door_cycle(self):
        """WEAR-1: floors travelled (B1 <-> 1 counts as one floor), motor starts, door cycles."""
        self.sim.submit_car_call("E1", 6)
        self.sim.advance(10)
        self.sim.submit_car_call("E1", -1)
        self.sim.advance(10)
        wear = self.sim.wear.cars["E1"]
        self.assertEqual(wear.floors_month, 5 + 6)
        self.assertEqual(wear.starts_month, 2)
        self.assertEqual(wear.doors_month, 2)
        self.assertEqual(self.sim.fleet_status()["elevators"][0]["floors_month"], 11)

    def test_dispatch_prefers_less_travelled_car_when_service_times_are_close(self):
        """WEAR-3: with equal predicted service time the least-used car of the group wins."""
        for eid in ("E1", "E2", "E3"):
            self.sim.wear.record_move(eid, 5_000)
        request, _ = self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertEqual(request["assigned_car"], "E4")

    def test_balancing_never_costs_more_than_the_penalty_cap(self):
        """A much closer car still wins: wait-time targets (PERF-9) stay protected."""
        self.sim.wear.record_move("E2", 50_000)  # E2 heavily used -> capped penalty
        self.sim.decision_trace = []
        cap = self.sim.wear.config.max_penalty_seconds
        self.sim.cars["E2"].current_floor = 20
        request, _ = self.sim.submit_hall_call(origin_floor=21, direction="UP")
        # E2 ETA 1 s + capped penalty (15 s) = 16 s < E1/E3/E4 at floor 1: ETA 20 s -> E2 still wins.
        self.assertEqual(request["assigned_car"], "E2")
        row = next(c for c in self.sim.decision_trace[-1]["candidates"] if c["car"] == "E2")
        self.assertAlmostEqual(row["penalty"], cap)
        # With the old 30 s cap the much closer car would have lost (31 s > 20 s).
        self.assertLess(1 + cap, 20)

    def test_penalty_pauses_while_the_group_is_busy(self):
        """WEAR-3 busy rule: more active hall calls than eligible cars -> no wear penalty."""
        self.sim.wear.record_move("E1", 50_000)
        self.sim.decision_trace = []
        self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertFalse(self.sim.decision_trace[-1]["busy_gate"])
        self.assertGreater(max(c["penalty"] for c in self.sim.decision_trace[-1]["candidates"]), 0)
        for f in (12, 14, 16, 18):
            self.sim.submit_hall_call(origin_floor=f, direction="UP")
        last = self.sim.decision_trace[-1]
        self.assertTrue(last["busy_gate"])
        self.assertTrue(all(c["penalty"] == 0 for c in last["candidates"]))

    def test_balancing_off_restores_original_lowest_id_tie_break(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False,
                                   wear_config=WearConfig(balancing_enabled=False))
        sim.reset(base_time=JAN)
        for eid in ("E1", "E2", "E3"):
            sim.wear.record_move(eid, 5_000)
        request, _ = sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertEqual(request["assigned_car"], "E1")

    def test_month_end_closes_window_and_logs_event(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False, wear_config=WearConfig())
        sim.reset(base_time=datetime(2026, 1, 31, 23, 59, 0, tzinfo=timezone.utc))
        sim.submit_car_call("E1", 10)
        sim.advance(120)
        self.assertEqual(sim.wear.month_start, datetime(2026, 2, 1, tzinfo=timezone.utc))
        closed = [r for r in sim.wear.month_history if r["wear_group"] == "RESIDENTIAL"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["per_car_floors"]["E1"], 9)
        self.assertFalse(closed[0]["within_limit"])  # only E1 moved: 100 % spread
        self.assertTrue(any(e["event_type"] == "WEAR_LIMIT_EXCEEDED" for e in sim.events))
        status = sim.wear_status()
        self.assertEqual(status["month"]["start"], "2026-02-01T00:00:00+00:00")
        self.assertEqual(status["cars"][0]["floors_period"], 9)

    def test_idle_fast_forward_still_resets_counters_after_six_months(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False, wear_config=WearConfig())
        sim.reset(base_time=JAN)
        sim.submit_car_call("E2", 3)
        sim.advance(10)
        for _ in range(7):
            sim.step_from_clock(31 * 24 * 3600)
        self.assertEqual(len([p for p in sim.wear.period_history if p["wear_group"] == "RESIDENTIAL"]), 1)
        self.assertEqual(sim.wear.cars["E2"].floors_period, 0)
        self.assertEqual(sim.wear.cars["E2"].floors_lifetime, 2)
        self.assertTrue(any(e["event_type"] == "TRIP_COUNTER_RESET" for e in sim.events))


class TestCollectiveControlAndBoarding(unittest.TestCase):
    def setUp(self):
        self.sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        self.sim.reset(base_time=JAN)

    def test_car_calls_are_served_in_travel_order(self):
        for floor in (25, 5, 12):
            self.sim.submit_car_call("E1", floor)
        self.assertEqual([s.floor for s in self.sim.cars["E1"].stops], [5, 12, 25])
        self.sim.advance(60)
        self.assertEqual(self.sim.cars["E1"].current_floor, 25)
        self.assertEqual(self.sim.wear.cars["E1"].floors_month, 24)  # no zig-zag

    def test_hall_call_on_the_way_is_picked_up_without_detour(self):
        self.sim.submit_car_call("E1", 20)
        self.sim.cars["E1"].current_floor = 4
        for eid in ("E2", "E3", "E4"):
            self.sim.cars[eid].current_floor = 30
        request, _ = self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertEqual(request["assigned_car"], "E1")
        self.assertEqual([s.floor for s in self.sim.cars["E1"].stops], [10, 20])

    def test_car_call_after_arrival_is_boarding_evidence(self):
        """FR-1.21: a car call that follows the arrival counts as boarding evidence."""
        request, _ = self.sim.submit_hall_call(origin_floor=4, direction="UP")
        self.sim.advance(3)
        self.assertEqual(self.sim.hall_calls[request["request_id"]].status, "COMPLETED")
        self.sim.submit_car_call(request["assigned_car"], 9)
        self.assertTrue(self.sim.hall_calls[request["request_id"]].boarding_evidence)
        self.sim.advance(40)
        self.assertFalse(self.sim.hall_calls[request["request_id"]].no_boarding)


class _WearFakeConn:
    """Captures the SQL written by ``sync_to_db`` (no PostgreSQL needed)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.history: dict[str, dict] = {}
        self.hall_writes = 0

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO hall_calls"):
            self.hall_writes += 1
        elif q.startswith("INSERT INTO elevator_wear_counters"):
            names = ["elevator_id", "wear_group", "period_start", "month_start", "floors_month",
                     "floors_period", "floors_lifetime", "starts_month", "starts_period",
                     "starts_lifetime", "doors_month", "doors_period", "doors_lifetime", "updated_at"]
            self.rows[args[0]] = dict(zip(names, args))
        elif q.startswith("INSERT INTO elevator_wear_history"):
            names = ["window_id", "window_type", "wear_group", "window_start", "window_end",
                     "per_car_floors", "spread_pct", "limit_pct", "within_limit", "closed_at"]
            self.history.setdefault(args[0], dict(zip(names, args)))
        return "INSERT 0 1"

    async def executemany(self, query: str, rows):
        return None

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "FROM elevator_wear_counters" in q:
            return list(self.rows.values())
        if "FROM elevator_wear_history" in q:
            return list(self.history.values())
        return []


class TestWearPersistence(unittest.TestCase):
    def test_counters_survive_restart_and_missed_month_is_closed(self):
        """WEAR-6: counters persist across a SREMS restart; a month that ended while the
        system was down is closed on start-up from the last synced counters."""
        sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        sim.reset(base_time=datetime(2025, 12, 30, tzinfo=timezone.utc))
        sim.submit_car_call("E3", 15)
        sim.advance(30)
        conn = _WearFakeConn()
        asyncio.run(sim.sync_to_db(conn))
        self.assertEqual(conn.rows["E3"]["floors_month"], 14)
        restored = OperationalSimulator(auto_advance=False, load_from_db=False)
        self.assertTrue(asyncio.run(restored.load_from_db(conn)))
        # load_from_db anchors to wall-clock "now" (after Dec 2025): December was closed.
        self.assertEqual(restored.wear.cars["E3"].floors_lifetime, 14)
        dec = [r for r in restored.wear.month_history
               if r["window_start"] == datetime(2025, 12, 1, tzinfo=timezone.utc)
               and r["wear_group"] == "RESIDENTIAL"]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["per_car_floors"]["E3"], 14)
        self.assertEqual(restored.wear.cars["E3"].floors_month, 0)

    def test_incremental_sync_skips_unchanged_requests(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        sim.reset(base_time=JAN)
        for floor in (3, 7, 11):
            sim.submit_hall_call(origin_floor=floor, direction="UP")
        sim.advance(60)
        conn = _WearFakeConn()
        asyncio.run(sim.sync_to_db(conn))
        first = conn.hall_writes
        self.assertEqual(first, 3)
        asyncio.run(sim.sync_to_db(conn))
        self.assertEqual(conn.hall_writes, first)  # nothing changed -> nothing re-written


class TestWearBenchmark(unittest.TestCase):
    """VT-33 (short form): realistic journeys, balancing ON vs OFF on the same seed."""

    def test_balancing_keeps_spread_small_where_baseline_drifts(self):
        start = datetime(2026, 3, 2, tzinfo=timezone.utc)
        on = run_wear_benchmark(WearBenchmarkConfig(days=2, start=start, seed=7), label="on")
        off = run_wear_benchmark(WearBenchmarkConfig(days=2, start=start, seed=7,
                                                     balancing_enabled=False), label="off")
        self.assertLess(on.verdict["month_to_date_spread_pct"], 1.0)
        self.assertGreater(off.verdict["month_to_date_spread_pct"], 5.0)
        self.assertTrue(on.verdict["perf9_peak_mean_ok"])
        self.assertTrue(on.verdict["perf9_peak_p95_ok"])
        # BEN-2 repeatability.
        again = run_wear_benchmark(WearBenchmarkConfig(days=2, start=start, seed=7), label="on")
        self.assertEqual(again.per_car_floors_total, on.per_car_floors_total)


if __name__ == "__main__":
    unittest.main()
