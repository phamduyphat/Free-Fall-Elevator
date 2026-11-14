"""Database-free safety and reliability *software test-stub* regression tests."""
import unittest

from simulation.operational import OperationalSimulator, OperationalError


class TestIncidentsAndRecovery(unittest.TestCase):
    def setUp(self):
        self.sim = OperationalSimulator(auto_advance=False)

    def test_help_raises_alert_but_car_keeps_serving(self):
        before, _ = self.sim.submit_hall_call(origin_floor=7, direction="UP")
        result = self.sim.trigger_emergency("E1", "HELP")
        self.assertEqual(result["kind"], "HELP")
        self.assertEqual(self.sim.cars["E1"].availability, "IN_SERVICE")
        self.assertEqual(self.sim.hall_call_status(before["request_id"])["assigned_car"], "E1")
        self.assertEqual(len(self.sim.list_incidents()), 1)

    def test_admin_confirmed_stuck_requeues_hall(self):
        hall, _ = self.sim.submit_hall_call(origin_floor=14, direction="UP")
        help_incident = self.sim.trigger_emergency("E1", "HELP")
        confirmed = self.sim.confirm_help_as_stuck(help_incident["incident_id"])
        self.assertEqual(confirmed["kind"], "STUCK_CAR")
        self.assertEqual(self.sim.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")
        self.assertEqual(self.sim.hall_call_status(hall["request_id"])["assigned_car"], "E2")
        with self.assertRaisesRegex(OperationalError, "Unresolved incident"):
            self.sim.isolate("E1", "IN_SERVICE")

    def test_medical_nonstop_to_responder_floor(self):
        self.sim.cars["E1"].current_floor = 8
        hall, _ = self.sim.submit_hall_call(origin_floor=12, direction="UP")
        medical = self.sim.trigger_emergency("E1", "MEDICAL")
        self.assertEqual(self.sim.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")
        self.assertTrue(self.sim.cars["E1"].medical_run)
        self.assertEqual(self.sim.hall_call_status(hall["request_id"])["assigned_car"], "E2")
        self.sim.advance(7)
        self.assertEqual(self.sim.cars["E1"].current_floor, 1)
        self.assertEqual(self.sim.cars["E1"].door_state, "OPEN")
        self.assertEqual(self.sim.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")
        self.assertFalse(self.sim.cars["E1"].medical_run)
        self.sim.incident_action(medical["incident_id"], "ACKNOWLEDGE", actor="ADMIN")
        self.sim.incident_action(medical["incident_id"], "RESOLVE", actor="ADMIN",
                                 technician_confirmation=True)
        self.assertEqual(self.sim.cars["E1"].availability, "IN_SERVICE")

    def test_building_fire_suspends_dispatch_no_recall_and_recovery(self):
        self.sim.cars["E2"].current_floor = 10
        incident = self.sim.building_emergency("FIRE")
        self.assertEqual(self.sim.cars["E2"].current_floor, 10)
        self.assertTrue(all(car.availability == "EMERGENCY_UNAVAILABLE"
                            for car in self.sim.cars.values()))
        hall, _ = self.sim.submit_hall_call(origin_floor=5, direction="UP")
        self.assertEqual(hall["status"], "QUEUED")
        self.sim.advance(4)
        self.assertEqual(self.sim.cars["E2"].current_floor, 10)
        self.sim.incident_action(incident["incident_id"], "RESOLVE", actor="ADMIN",
                                 technician_confirmation=True)
        self.assertTrue(all(car.availability == "IN_SERVICE" for car in self.sim.cars.values()))
        self.assertIsNotNone(self.sim.hall_call_status(hall["request_id"])["assigned_car"])

    def test_blocking_fault_needs_clear_then_confirmation(self):
        incident = self.sim.report_fault("E4", "MOTOR_FAULT")
        self.assertFalse(self.sim.recoverable("E4"))
        with self.assertRaisesRegex(OperationalError, "Recovery checks"):
            self.sim.incident_action(incident["incident_id"], "RESOLVE", actor="TECH",
                                     technician_confirmation=True)
        self.sim.clear_fault("E4")
        self.assertEqual(self.sim.cars["E4"].availability, "FAULT")
        self.sim.incident_action(incident["incident_id"], "RESOLVE", actor="TECH",
                                 technician_confirmation=True)
        self.assertEqual(self.sim.cars["E4"].availability, "IN_SERVICE")
        self.sim.incident_action(incident["incident_id"], "CLOSE", actor="TECH")

    def test_communication_loss_requeues_and_no_blind_retry(self):
        hall, _ = self.sim.submit_hall_call(origin_floor=19, direction="UP")
        trip = self.sim.submit_car_call("E1", 9)
        incident = self.sim.communication_loss("E1")
        self.assertEqual(self.sim.cars["E1"].availability, "STALE")
        self.assertEqual(self.sim.car_calls[trip["request_id"]].status, "BLOCKED/REQUIRES_RECONCILIATION")
        self.assertNotEqual(self.sim.hall_call_status(hall["request_id"])["assigned_car"], "E1")
        self.sim.reconcile_connection("E1")
        self.assertEqual(self.sim.car_calls[trip["request_id"]].status, "FAILED")
        self.assertEqual(self.sim.cars["E1"].availability, "IN_SERVICE")
        self.sim.incident_action(incident["incident_id"], "RESOLVE", actor="TECH",
                                 technician_confirmation=True)

    def test_obstruction_inhibits_close_and_stops_motion_until_clear(self):
        self.sim.submit_car_call("E1", 4)
        self.sim.door_obstruction("E1", True)
        with self.assertRaisesRegex(OperationalError, "prohibited"):
            self.sim.passenger_door_command("E1", "CLOSE")
        self.sim.advance(10)
        self.assertEqual(self.sim.cars["E1"].current_floor, 1)
        self.assertEqual(len([i for i in self.sim.incidents.values()
                              if i.kind == "DOOR_OBSTRUCTION"]), 1)
        self.sim.door_obstruction("E1", False)
        self.sim.advance(3)
        self.assertEqual(self.sim.cars["E1"].current_floor, 4)

    def test_admin_phone_rescue_and_boarding(self):
        incident = self.sim.phone_medical_incident(10)
        with self.assertRaises(OperationalError):
            self.sim.dispatch_rescue(incident["incident_id"])
        rescue = self.sim.dispatch_rescue(incident["incident_id"],
                                          confirmed_empty=True, wait_seconds=15)
        self.assertEqual(rescue["rescue_car_id"], "E1")
        self.assertFalse(self.sim._eligible(self.sim.cars["E1"]))
        self.sim.advance(9)
        self.assertEqual(self.sim.cars["E1"].current_floor, 10)
        self.assertEqual(self.sim.cars["E1"].door_state, "OPEN")
        self.sim.confirm_rescue_boarding(incident["incident_id"])
        self.sim.advance(9)
        self.assertEqual(self.sim.cars["E1"].current_floor, 1)
        self.assertEqual(self.sim.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")

    def test_rescue_wait_expiry_releases_without_closing_incident(self):
        incident = self.sim.phone_medical_incident(2)
        self.sim.dispatch_rescue(incident["incident_id"], confirmed_empty=True, wait_seconds=3)
        self.sim.advance(4)
        self.assertFalse(self.sim.cars["E1"].rescue_active)
        self.assertEqual(self.sim.incidents[incident["incident_id"]].status, "OPEN")

    def test_repeated_trigger_and_incident_escalation(self):
        for _ in range(4):
            self.sim.trigger_emergency("E3", "HELP")
        self.assertEqual(sum(i.kind == "REPEATED_EMERGENCY" for i in self.sim.incidents.values()), 1)
        self.sim.advance(60)
        self.assertTrue(any(i.escalated for i in self.sim.incidents.values()))

    def test_metrics_are_aggregates_labelled_as_stub(self):
        self.sim.submit_hall_call(origin_floor=3, direction="UP")
        self.sim.advance(2)
        metrics = self.sim.metrics()
        self.assertEqual(metrics["source"], "labelled-test-stub")
        self.assertEqual(metrics["completed_hall_calls"], 1)
        self.assertEqual(metrics["mean_wait_seconds"], 2)
        self.assertEqual(metrics["p95_wait_seconds"], 2)
        self.assertEqual(metrics["fleet_count"], 5)


if __name__ == "__main__":
    unittest.main()
