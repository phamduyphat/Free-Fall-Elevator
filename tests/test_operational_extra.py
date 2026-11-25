"""Additional database-free Mode-1 lifecycle and diagnostics tests."""
import unittest

from simulation.operational import OperationalError, OperationalSimulator


class AdditionalOperationalTests(unittest.TestCase):
    def setUp(self):
        self.sim = OperationalSimulator(auto_advance=False)

    def test_no_boarding_flag_needs_explicit_stub_evidence(self):
        request, _ = self.sim.submit_hall_call(origin_floor=2, direction="UP")
        self.sim.advance(1)
        self.sim.advance(30)
        self.assertFalse(self.sim.hall_call_status(request["request_id"])["no_boarding"])

    def test_evidence_false_sets_no_boarding_after_30_seconds(self):
        request, _ = self.sim.submit_hall_call(origin_floor=2, direction="UP")
        self.sim.advance(1)
        self.sim.boarding_evidence(request["request_id"], False)
        self.sim.advance(30)
        result = self.sim.hall_call_status(request["request_id"])
        self.assertTrue(result["no_boarding"])
        self.assertEqual(result["wait_seconds"], 1.0)

    def test_evidence_true_does_not_mark_no_boarding(self):
        request, _ = self.sim.submit_hall_call(origin_floor=2, direction="UP")
        self.sim.advance(1)
        self.sim.boarding_evidence(request["request_id"], True)
        self.sim.advance(40)
        self.assertFalse(self.sim.hall_call_status(request["request_id"])["no_boarding"])

    def test_repeated_no_boarding_creates_low_severity_incident(self):
        for _ in range(5):
            self.sim.cars["E1"].current_floor = 2
            request, _ = self.sim.submit_hall_call(origin_floor=2, direction="UP")
            self.sim.advance(1)
            self.sim.boarding_evidence(request["request_id"], False)
            self.sim.advance(30)
        low = [i for i in self.sim.list_incidents() if i["kind"] == "REPEATED_EMPTY_CALLS"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["severity"], "LOW")

    def test_cancel_removes_stop_and_cannot_cancel_completed(self):
        request, _ = self.sim.submit_hall_call(origin_floor=8, direction="UP")
        self.sim.cancel_hall_call(request["request_id"], "admin")
        self.assertEqual(self.sim.hall_call_status(request["request_id"])["status"], "CANCELLED")
        self.assertFalse(self.sim.cars["E1"].stops)
        with self.assertRaises(OperationalError):
            self.sim.cancel_hall_call(request["request_id"], "admin")

    def test_diagnostics_is_labelled_and_shows_fault(self):
        self.sim.report_fault("E3", "DOOR_SENSOR")
        report = self.sim.diagnostics("E3")
        self.assertEqual(report["source"], "labelled-test-stub")
        self.assertEqual(report["car"]["active_fault"], "DOOR_SENSOR")
        self.assertFalse(report["recovery_ready"])

    def test_sync_to_db_persists_simulation_events(self):
        import asyncio

        class MockConn:
            def __init__(self):
                self.calls = []

            async def execute(self, query, *args):
                self.calls.append((query.strip().split()[0].upper(), args))

        conn = MockConn()
        req, _ = self.sim.submit_hall_call(origin_floor=5, direction="UP")
        self.sim.advance(4)  # Car moves and arrives, completes hall call, generates trip
        self.sim.trigger_emergency("E1", "HELP")
        self.sim.isolate("E2", "MAINTENANCE")

        asyncio.run(self.sim.sync_to_db(conn))
        self.assertTrue(len(conn.calls) >= 3)
        self.assertTrue(all(c[0] == "INSERT" for c in conn.calls))

    def test_timestamps_are_aware_utc_for_clock_and_db_sync(self):
        import asyncio
        from datetime import datetime, timezone

        # Default reset and naive base_time both normalize to offset-aware UTC
        self.assertEqual(self.sim.base_time.tzinfo, timezone.utc)
        self.sim.reset(base_time=datetime(2026, 6, 1, 12, 0, 0))
        self.assertEqual(self.sim.base_time.tzinfo, timezone.utc)
        self.assertEqual(self.sim.base_time, datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

        # Even if a naive timestamp is injected manually, sync_to_db normalizes it to aware UTC
        self.sim.submit_hall_call(origin_floor=3, direction="UP")
        self.sim.advance(5)
        self.sim.submit_car_call("E1", 6)
        self.sim.advance(5)
        for c in self.sim.car_calls.values():
            c.accepted_at = c.accepted_at.replace(tzinfo=None)
            if c.completed_at is not None:
                c.completed_at = c.completed_at.replace(tzinfo=None)

        recorded_args = []

        class CaptureConn:
            async def execute(self, query, *args):
                recorded_args.extend(a for a in args if isinstance(a, datetime))

        asyncio.run(self.sim.sync_to_db(CaptureConn()))
        self.assertTrue(recorded_args)
        self.assertTrue(all(dt.tzinfo == timezone.utc for dt in recorded_args))


if __name__ == "__main__":
    unittest.main()
