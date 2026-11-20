"""Unit and benchmark tests for elevator wear-and-tear distance balancing.

Verifies:
- Maximum difference in accumulated floors across E1-E4 is kept under 1.8% a month.
- Maximum difference in accumulated floors across E1-E4 is kept under 4.0% in 6 months.
- Dual trip counters (1-month reset and 6-month reset).
- Deterministic reproducibility using random seeds.
- All 5 configurable strategies (BOTTOM_PRIORITY, TOP_HALT, LEAST_RUN_FIRST,
  OFF_PEAK_REBALANCE, OUTLIER_CORRECTION).
"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from simulation.wear_tear import (
    STRATEGY_DESCRIPTIONS,
    WearTearConfig,
    calculate_wear_metrics,
    detect_outlier,
    run_wear_tear_benchmark,
    select_best_car_for_wear,
)


class MockCar:
    def __init__(self, elevator_id: str):
        self.elevator_id = elevator_id


class TestWearTearBalancing(unittest.TestCase):
    def test_config_and_defaults(self):
        cfg = WearTearConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.strategy, "BOTTOM_PRIORITY")
        self.assertEqual(cfg.monthly_target_pct, 1.8)
        self.assertEqual(cfg.monthly_ceiling_pct, 2.0)
        self.assertEqual(cfg.six_month_target_pct, 4.0)
        self.assertEqual(cfg.six_month_ceiling_pct, 5.0)
        self.assertEqual(cfg.safety_margin_pct, 0.2)
        self.assertEqual(cfg.off_peak_start_hour, 22)
        self.assertEqual(cfg.off_peak_end_hour, 5)

        # Deserialization and fallback
        parsed = WearTearConfig.from_dict({
            "enabled": False,
            "strategy": "least_run_first",
            "monthly_target_pct": 1.5,
        })
        self.assertFalse(parsed.enabled)
        self.assertEqual(parsed.strategy, "LEAST_RUN_FIRST")
        self.assertEqual(parsed.monthly_target_pct, 1.5)

        invalid_strategy = WearTearConfig.from_dict({"strategy": "UNKNOWN_STRAT"})
        self.assertEqual(invalid_strategy.strategy, "BOTTOM_PRIORITY")

    def test_wear_metrics_calculation(self):
        distances = {
            "E1": 1000,
            "E2": 1005,
            "E3": 995,
            "E4": 1002,
            "E5": 400,  # Freight car excluded from residential group
        }
        cfg = WearTearConfig()
        metrics = calculate_wear_metrics(distances, cfg)
        self.assertEqual(metrics.min_distance, 995)
        self.assertEqual(metrics.max_distance, 1005)
        self.assertEqual(metrics.delta_floors, 10)
        # 10 / 1005 * 100 = ~0.995%
        self.assertAlmostEqual(metrics.spread_pct, 0.995, places=2)
        self.assertTrue(metrics.monthly_target_met)
        self.assertTrue(metrics.six_month_target_met)

    def test_outlier_detection(self):
        # E3 is significantly lagging behind E1, E2, E4
        distances = {"E1": 1000, "E2": 1002, "E3": 950, "E4": 1001}
        car, outlier_type = detect_outlier(distances, ["E1", "E2", "E3", "E4"])
        self.assertEqual(car, "E3")
        self.assertEqual(outlier_type, "LOW")

        # E2 has run significantly more than E1, E3, E4
        distances_high = {"E1": 1000, "E2": 1050, "E3": 1001, "E4": 1002}
        car_high, type_high = detect_outlier(distances_high, ["E1", "E2", "E3", "E4"])
        self.assertEqual(car_high, "E2")
        self.assertEqual(type_high, "HIGH")

        # Balanced cluster: no outlier
        balanced = {"E1": 1000, "E2": 1005, "E3": 998, "E4": 1002}
        car_none, type_none = detect_outlier(balanced, ["E1", "E2", "E3", "E4"])
        self.assertIsNone(car_none)
        self.assertIsNone(type_none)

    def test_strategy_1_bottom_priority(self):
        cfg = WearTearConfig(strategy="BOTTOM_PRIORITY", monthly_target_pct=1.8, safety_margin_pct=0.2)
        cars = [MockCar("E1"), MockCar("E2"), MockCar("E3"), MockCar("E4")]
        distances = {"E1": 1000, "E2": 1000, "E3": 980, "E4": 1000}
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        eta = {"E1": 2, "E2": 2, "E3": 5, "E4": 2}

        # E3 is lagging (spread is 20 / 1000 = 2.0% >= 1.6% trigger)
        # Even with slightly higher ETA, E3 gets priority until within safety margin
        chosen = select_best_car_for_wear(cars, distances, cfg, dt, eta)
        self.assertEqual(chosen.elevator_id, "E3")

    def test_strategy_2_top_halt(self):
        cfg = WearTearConfig(strategy="TOP_HALT", monthly_target_pct=1.8, safety_margin_pct=0.2)
        cars = [MockCar("E1"), MockCar("E2"), MockCar("E3"), MockCar("E4")]
        # E1 is at top threshold (1020 vs 1000 is 1.96% spread)
        distances = {"E1": 1020, "E2": 1000, "E3": 1000, "E4": 1000}
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        eta = {"E1": 1, "E2": 3, "E3": 4, "E4": 4}

        # E1 has best ETA but is halted to prevent wear violation; E2 takes call
        chosen = select_best_car_for_wear(cars, distances, cfg, dt, eta)
        self.assertEqual(chosen.elevator_id, "E2")

    def test_strategy_3_least_run_first(self):
        cfg = WearTearConfig(strategy="LEAST_RUN_FIRST")
        cars = [MockCar("E1"), MockCar("E2"), MockCar("E3"), MockCar("E4")]
        distances = {"E1": 500, "E2": 450, "E3": 480, "E4": 510}
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # E2 has run the least (450) and is chosen
        chosen = select_best_car_for_wear(cars, distances, cfg, dt)
        self.assertEqual(chosen.elevator_id, "E2")

    def test_strategy_4_off_peak_rebalance(self):
        cfg = WearTearConfig(strategy="OFF_PEAK_REBALANCE", off_peak_start_hour=22, off_peak_end_hour=5)
        cars = [MockCar("E1"), MockCar("E2"), MockCar("E3"), MockCar("E4")]
        distances = {"E1": 600, "E2": 600, "E3": 550, "E4": 600}

        # At night (23:00 / 11 PM), off-peak rebalancing routes strictly to lowest distance (E3)
        night_dt = datetime(2026, 1, 1, 23, 0, 0, tzinfo=timezone.utc)
        night_chosen = select_best_car_for_wear(cars, distances, cfg, night_dt)
        self.assertEqual(night_chosen.elevator_id, "E3")

        # At day peak (08:00 AM), standard ETA dispatch takes precedence
        day_dt = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        eta = {"E1": 1, "E2": 4, "E3": 6, "E4": 5}
        day_chosen = select_best_car_for_wear(cars, distances, cfg, day_dt, eta)
        self.assertEqual(day_chosen.elevator_id, "E1")

    def test_strategy_5_outlier_correction(self):
        cfg = WearTearConfig(strategy="OUTLIER_CORRECTION")
        cars = [MockCar("E1"), MockCar("E2"), MockCar("E3"), MockCar("E4")]
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Low outlier (E4 is lagging)
        distances_low = {"E1": 800, "E2": 805, "E3": 802, "E4": 750}
        chosen_low = select_best_car_for_wear(cars, distances_low, cfg, dt)
        self.assertEqual(chosen_low.elevator_id, "E4")

        # High outlier (E1 has run too much)
        distances_high = {"E1": 850, "E2": 800, "E3": 801, "E4": 802}
        eta = {"E1": 1, "E2": 3, "E3": 4, "E4": 5}
        chosen_high = select_best_car_for_wear(cars, distances_high, cfg, dt, eta)
        # E1 is halted for maintenance; next best non-outlier is chosen
        self.assertNotEqual(chosen_high.elevator_id, "E1")
        self.assertEqual(chosen_high.elevator_id, "E2")

    def test_reproducible_6_month_benchmark(self):
        """Runs the 6-month synthetic random load benchmark with reproducible seed."""
        # Run with default strategy (BOTTOM_PRIORITY)
        res = run_wear_tear_benchmark(
            seed=42,
            months=6,
            days_per_month=30,
            calls_per_day=150,
            config=WearTearConfig(strategy="BOTTOM_PRIORITY"),
            compare_baseline=True,
        )

        self.assertEqual(res.simulated_months, 6)
        self.assertEqual(res.simulated_days, 180)
        self.assertEqual(len(res.time_series), 180)

        # Verify strict threshold compliance
        # 1. Monthly spread <= 1.8% (requirement: < 2.0%)
        self.assertLessEqual(res.monthly_spread_pct, 1.8, f"Monthly spread {res.monthly_spread_pct}% exceeds 1.8%")
        self.assertTrue(res.monthly_target_met)
        self.assertTrue(res.monthly_requirement_met)

        # 2. 6-Month spread <= 4.0% (requirement: <= 5.0%)
        self.assertLessEqual(res.six_month_spread_pct, 4.0, f"6-month spread {res.six_month_spread_pct}% exceeds 4.0%")
        self.assertTrue(res.six_month_target_met)
        self.assertTrue(res.six_month_requirement_met)

        # 3. Deterministic reproducibility: rerun with same seed produces exact identical outputs
        res_repeat = run_wear_tear_benchmark(
            seed=42,
            months=6,
            days_per_month=30,
            calls_per_day=150,
            config=WearTearConfig(strategy="BOTTOM_PRIORITY"),
            compare_baseline=False,
        )
        self.assertEqual(res.car_floors, res_repeat.car_floors)
        self.assertEqual(res.car_trips, res_repeat.car_trips)
        self.assertEqual(res.six_month_spread_pct, res_repeat.six_month_spread_pct)

        # 4. Baseline comparison: unmitigated baseline spread should be significantly higher
        self.assertIsNotNone(res.baseline_comparison)
        baseline = res.baseline_comparison
        self.assertGreater(baseline["unmitigated_six_month_spread_pct"], res.six_month_spread_pct)
        self.assertGreater(baseline["improvement_pct_points"], 0)

    def test_all_strategies_satisfy_requirements(self):
        """Verify each of the 5 strategies keeps spread within the specified bounds."""
        for strat in ("BOTTOM_PRIORITY", "TOP_HALT", "LEAST_RUN_FIRST", "OFF_PEAK_REBALANCE", "OUTLIER_CORRECTION"):
            cfg = WearTearConfig(strategy=strat)
            res = run_wear_tear_benchmark(
                seed=101,
                months=6,
                days_per_month=30,
                calls_per_day=120,
                config=cfg,
                compare_baseline=False,
            )
            # All strategies must meet the non-functional requirement ceiling (<= 2.0% monthly, <= 5.0% 6-month)
            self.assertTrue(
                res.monthly_requirement_met,
                f"Strategy {strat} failed monthly requirement: {res.monthly_spread_pct}%",
            )
            self.assertTrue(
                res.six_month_requirement_met,
                f"Strategy {strat} failed 6-month requirement: {res.six_month_spread_pct}%",
            )

    def test_wear_tear_api_endpoints(self):
        """Test the FastAPI operational wear-and-tear endpoints."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app, raise_server_exceptions=False)

        # GET config
        resp = client.get("/api/v1/operational/wear-tear/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("enabled", data)
        self.assertIn("strategy", data)

        # POST config update
        resp2 = client.post("/api/v1/operational/wear-tear/config", json={
            "strategy": "TOP_HALT",
            "safety_margin_pct": 0.25,
        })
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["strategy"], "TOP_HALT")
        self.assertEqual(data2["safety_margin_pct"], 0.25)

        # GET status
        resp3 = client.get("/api/v1/operational/wear-tear/status")
        self.assertEqual(resp3.status_code, 200)
        status_data = resp3.json()
        self.assertIn("car_stats", status_data)
        self.assertIn("monthly", status_data)
        self.assertIn("six_months", status_data)
        self.assertIn("E1", status_data["car_stats"])

        # POST reset
        resp4 = client.post("/api/v1/operational/wear-tear/reset", json={"scope": "ALL"})
        self.assertEqual(resp4.status_code, 200)

        # POST benchmark
        resp5 = client.post("/api/v1/operational/wear-tear/benchmark", json={
            "seed": 999,
            "months": 1,
            "days_per_month": 15,
            "calls_per_day": 50,
            "strategy": "LEAST_RUN_FIRST",
            "compare_baseline": True,
        })
        self.assertEqual(resp5.status_code, 200)
        bench_data = resp5.json()
        self.assertEqual(bench_data["strategy"], "LEAST_RUN_FIRST")
        self.assertIn("time_series", bench_data)
        self.assertIn("baseline_comparison", bench_data)
        self.assertTrue(len(bench_data["time_series"]) > 0)

    def test_safe_state_sparsely_spreads_burst_workload(self):
        class MockCarWithStops:
            def __init__(self, elevator_id: str):
                self.elevator_id = elevator_id
                self.stops = []

        cfg = WearTearConfig(strategy="BOTTOM_PRIORITY")
        cars = [MockCarWithStops("E1"), MockCarWithStops("E2"), MockCarWithStops("E3"), MockCarWithStops("E4")]
        distances = {"E1": 1000, "E2": 1000, "E3": 1000, "E4": 1000}
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        eta = {"E1": 5, "E2": 5, "E3": 5, "E4": 5}

        # Sequence of 4 burst calls when fleet is in safe state
        chosen_ids = []
        for _ in range(4):
            chosen = select_best_car_for_wear(cars, distances, cfg, dt, eta)
            chosen.stops.append("stop")
            chosen_ids.append(chosen.elevator_id)

        # Workload is sparsely spread across all 4 cars
        self.assertEqual(sorted(chosen_ids), ["E1", "E2", "E3", "E4"])

        # Imbalanced state: E3 is lagging behind safety threshold
        cfg_violation = WearTearConfig(strategy="BOTTOM_PRIORITY", monthly_target_pct=1.8, safety_margin_pct=0.2)
        imbalanced_cars = [MockCarWithStops("E1"), MockCarWithStops("E2"), MockCarWithStops("E3"), MockCarWithStops("E4")]
        imbalanced_distances = {"E1": 1000, "E2": 1000, "E3": 980, "E4": 1000}
        chosen_imbalanced = select_best_car_for_wear(imbalanced_cars, imbalanced_distances, cfg_violation, dt, eta)
        self.assertEqual(chosen_imbalanced.elevator_id, "E3")



if __name__ == "__main__":
    unittest.main()
