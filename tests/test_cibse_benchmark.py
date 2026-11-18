"""Automated test suite for CIBSE Guide D elevator benchmark runs (BEN-1..BEN-4, PERF-8..10, CAP-1)."""
from __future__ import annotations

import unittest

from simulation.cibse_benchmark import (
    CAP1_STRESS_BURST,
    CIBSE_EVENING_PEAK,
    CIBSE_MORNING_PEAK,
    CIBSE_OFF_PEAK,
    run_cibse_benchmark,
)
from simulation.operational import OperationalSimulator


class TestCIBSEBenchmark(unittest.TestCase):
    """Verify performance metrics against real-life CIBSE Guide D residential elevator standards."""

    def setUp(self) -> None:
        self.sim = OperationalSimulator(auto_advance=False)

    def test_ben1_and_perf9_morning_down_peak_meets_targets(self) -> None:
        """BEN-1 & PERF-9: Morning down-peak (70% down) average wait <= 60s, 95th percentile <= 120s."""
        report = run_cibse_benchmark(self.sim, CIBSE_MORNING_PEAK, reset_sim=True)

        self.assertGreater(report.total_calls_submitted, 50)
        self.assertEqual(report.completed_calls + report.active_calls_remaining, report.total_calls_submitted - report.duplicate_calls_merged)
        self.assertLessEqual(report.mean_wait_seconds, 60.0)
        self.assertLessEqual(report.p95_wait_seconds, 120.0)
        self.assertTrue(report.target_met)
        self.assertTrue(report.queue_bounded_pass)
        self.assertEqual(report.dropped_requests_count, 0)
        self.assertGreater(report.total_trips, 0)

    def test_ben1_and_perf9_evening_up_peak_meets_targets(self) -> None:
        """BEN-1 & PERF-9: Evening up-peak (70% up) average wait <= 60s, 95th percentile <= 120s."""
        report = run_cibse_benchmark(self.sim, CIBSE_EVENING_PEAK, reset_sim=True)

        self.assertGreater(report.total_calls_submitted, 50)
        self.assertLessEqual(report.mean_wait_seconds, 60.0)
        self.assertLessEqual(report.p95_wait_seconds, 120.0)
        self.assertTrue(report.target_met)
        self.assertTrue(report.queue_bounded_pass)

    def test_ben1_and_perf10_off_peak_meets_target(self) -> None:
        """BEN-1 & PERF-10: Off-peak average wait <= 90s."""
        report = run_cibse_benchmark(self.sim, CIBSE_OFF_PEAK, reset_sim=True)

        self.assertGreater(report.total_calls_submitted, 10)
        self.assertLessEqual(report.mean_wait_seconds, 90.0)
        self.assertTrue(report.target_met)

    def test_ben2_repeatability_with_same_seed(self) -> None:
        """BEN-2: The same scenario configuration must be replayable with identical outputs."""
        report_1 = run_cibse_benchmark(self.sim, CIBSE_MORNING_PEAK, reset_sim=True)
        report_2 = run_cibse_benchmark(self.sim, CIBSE_MORNING_PEAK, reset_sim=True)

        self.assertEqual(report_1.total_calls_submitted, report_2.total_calls_submitted)
        self.assertEqual(report_1.completed_calls, report_2.completed_calls)
        self.assertAlmostEqual(report_1.mean_wait_seconds, report_2.mean_wait_seconds, places=4)
        self.assertEqual(report_1.p95_wait_seconds, report_2.p95_wait_seconds)
        self.assertEqual(report_1.total_trips, report_2.total_trips)
        self.assertEqual(report_1.total_floors_traversed, report_2.total_floors_traversed)

    def test_cap1_and_perf5_stress_capacity_queue_bounded(self) -> None:
        """CAP-1 & PERF-5: Request queue remains bounded (<= 100) under intense burst load."""
        report = run_cibse_benchmark(self.sim, CAP1_STRESS_BURST, reset_sim=True)

        self.assertGreater(report.total_calls_submitted, 80)
        self.assertLessEqual(report.peak_simultaneous_active_queue, 100)
        self.assertTrue(report.queue_bounded_pass)
        self.assertEqual(report.dropped_requests_count, 0)


if __name__ == "__main__":
    unittest.main()
