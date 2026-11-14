"""Isolated, database-free Mode 1 behaviour tests (unittest compatible)."""

import unittest

from simulation.operational import OperationalError, OperationalSimulator, Stop


class TestOperationalSimulator(unittest.TestCase):
    def setUp(self):
        self.sim = OperationalSimulator(auto_advance=False)

    def test_registered_cars_and_test_stub_labelling(self):
        fleet = self.sim.fleet_status()
        self.assertEqual(fleet["source"], "labelled-test-stub")
        self.assertEqual([c["elevator_id"] for c in fleet["elevators"]],
                         ["E1", "E2", "E3", "E4", "E5"])
        self.assertTrue(all(c["current_floor"] == 1 for c in fleet["elevators"]))

    def test_deterministic_dispatch_and_duplicate_merging(self):
        request, merged = self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertFalse(merged)
        self.assertEqual(request["assigned_car"], "E1")
        same, merged = self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertTrue(merged)
        self.assertEqual(same["request_id"], request["request_id"])
        self.assertEqual(len(self.sim.hall_call_status()), 1)

    def test_direction_aware_dispatch_does_not_use_nearest_car_only(self):
        self.sim.cars["E1"].current_floor = 8
        self.sim.cars["E1"].stops = [Stop(1, "CAR", "old")]
        self.sim.cars["E2"].current_floor = 5
        self.sim.cars["E3"].current_floor = 1
        self.sim.cars["E4"].current_floor = 1
        # Nearest E1 is committed to travel downward to floor 1.
        request, _ = self.sim.submit_hall_call(origin_floor=10, direction="UP")
        self.assertEqual(request["assigned_car"], "E2")

    def test_completed_hall_request_wait_time_and_lifecycle(self):
        request, _ = self.sim.submit_hall_call(origin_floor=4, direction="UP")
        self.sim.advance(3)
        completed = self.sim.hall_call_status(request["request_id"])
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["wait_seconds"], 3.0)
        self.assertEqual(completed["history"], ["RECEIVED", "QUEUED", "ASSIGNED", "APPROACHING", "ARRIVED", "COMPLETED"])
        self.assertEqual(self.sim.cars["E1"].current_floor, 4)
        self.assertEqual(self.sim.cars["E1"].door_state, "OPEN")

    def test_maintenance_isolation_requeues_to_new_car(self):
        request, _ = self.sim.submit_hall_call(origin_floor=20, direction="UP")
        self.sim.advance(2)
        self.sim.isolate("E1", "MAINTENANCE")
        state = self.sim.hall_call_status(request["request_id"])
        self.assertEqual(state["assigned_car"], "E2")
        self.assertEqual(state["reassignment_count"], 1)
        self.assertEqual(state["history"].count("QUEUED"), 2)
        self.assertEqual(self.sim.cars["E1"].availability, "MAINTENANCE")
        self.sim.advance(19)
        self.assertEqual(self.sim.hall_call_status(request["request_id"])["status"], "COMPLETED")
        self.assertEqual(self.sim.hall_call_status(request["request_id"])["wait_seconds"], 21.0)

    def test_e5_restrictions_and_rejected_freight_when_unavailable(self):
        for bad in [dict(origin_floor=-1, call_type="FREIGHT"),
                    dict(origin_floor=-1, direction="DOWN"),
                    dict(origin_floor=30, direction="UP")]:
            with self.assertRaises(OperationalError):
                self.sim.submit_hall_call(**bad)
        accepted, _ = self.sim.submit_hall_call(origin_floor=15, call_type="FREIGHT")
        self.assertEqual(accepted["assigned_car"], "E5")
        self.assertEqual(accepted["direction"], None)
        with self.assertRaises(OperationalError):
            self.sim.submit_car_call("E5", -1)
        self.sim.isolate("E5", "MAINTENANCE")
        with self.assertRaisesRegex(OperationalError, "not queued"):
            self.sim.submit_hall_call(origin_floor=20, call_type="FREIGHT")

    def test_queued_call_when_all_residential_cars_unavailable(self):
        for eid in ["E1", "E2", "E3", "E4"]:
            self.sim.isolate(eid, "FAULT")
        request, _ = self.sim.submit_hall_call(origin_floor=5, direction="UP")
        self.assertEqual(request["status"], "QUEUED")
        self.assertIsNone(request["assigned_car"])
        self.sim.isolate("E3", "IN_SERVICE")
        self.assertEqual(self.sim.hall_call_status(request["request_id"])["assigned_car"], "E3")

    def test_e5_service_run_is_exclusive_and_releases_on_arrival(self):
        trip = self.sim.start_service_run(5)
        self.assertEqual(trip["status"], "QUEUED")
        self.assertEqual(self.sim.cars["E5"].availability, "OCCUPIED")
        with self.assertRaises(OperationalError):
            self.sim.submit_hall_call(origin_floor=10, call_type="FREIGHT")
        self.sim.advance(4)
        self.assertEqual(self.sim.car_calls[trip["request_id"]].status, "COMPLETED")
        self.assertEqual(self.sim.cars["E5"].availability, "IN_SERVICE")

    def test_b1_is_adjacent_to_first_floor_and_e5_cannot_go_there(self):
        self.sim.submit_car_call("E1", -1)
        self.sim.advance(1)
        self.assertEqual(self.sim.cars["E1"].current_floor, -1)
        with self.assertRaises(OperationalError):
            self.sim.submit_car_call("E5", -1)

    def test_invalid_elevator_or_floor_is_rejected(self):
        for eid, floor in [("E7", 3), ("E1", 0), ("E1", 31)]:
            with self.assertRaises(OperationalError):
                self.sim.submit_car_call(eid, floor)
        with self.assertRaises(OperationalError):
            self.sim.advance(-1)

    def test_unavailable_car_cannot_serve_authorized_destination(self):
        self.sim.isolate("E1", "EMERGENCY_UNAVAILABLE")
        with self.assertRaises(OperationalError):
            self.sim.submit_car_call("E1", 7)


if __name__ == "__main__":
    unittest.main()
