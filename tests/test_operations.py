"""Emergency, maintenance, and interface-recovery tests."""

import unittest
from datetime import datetime, timedelta, timezone

from srems import (
    Availability,
    Direction,
    DoorState,
    ElevatorState,
    RequestStatus,
    Role,
    SREMS,
)


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.system = SREMS()
        self.system.bootstrap_admin("admin-card", "admin")
        self.system.issue_credential(
            "admin-card", "tech-card", "tech", Role.TECHNICIAN
        )

    def test_maintenance_isolates_one_car_and_reassigns_request(self) -> None:
        request = self.system.request_hall_call(10, Direction.UP)
        original_car = request.assigned_elevator
        self.system.set_maintenance("tech-card", original_car, "inspection")
        self.assertNotEqual(request.assigned_elevator, original_car)
        self.assertEqual(self.system.states[original_car].availability, Availability.MAINTENANCE)

    def test_medical_emergency_is_unavailable_and_notifies(self) -> None:
        incident = self.system.trigger_emergency("MEDICAL", "E1")
        self.assertEqual(self.system.states["E1"].availability, Availability.EMERGENCY)
        self.assertEqual(incident.event_type, "MEDICAL")
        self.assertTrue(any(command.command == "medical_responder_run" for command in self.system.interface.commands))
        self.assertEqual(self.system.alerts[-1]["event_type"], "MEDICAL")

    def test_communication_loss_stops_commands_until_snapshot_reconciles(self) -> None:
        self.system.lose_communication()
        command_count = len(self.system.interface.commands)
        request = self.system.request_hall_call(6, Direction.DOWN)
        self.assertEqual(request.status, RequestStatus.QUEUED)
        self.assertEqual(len(self.system.interface.commands), command_count)
        self.assertTrue(all(state.stale for state in self.system.states.values()))
        outcome = self.system.restore_communication()
        self.assertTrue(all(outcome.values()))
        self.assertFalse(any(state.stale for state in self.system.states.values()))

    def test_obstruction_prevents_door_close_and_escalates(self) -> None:
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.system.set_obstruction("E1", True, now)
        command_count = len(self.system.interface.commands)
        self.assertFalse(self.system.control_door("E1", "close", now=now))
        self.assertEqual(len(self.system.interface.commands), command_count)
        self.assertEqual(
            self.system.check_obstruction_escalations(
                now + timedelta(seconds=11)
            ),
            ["E1"],
        )

    def test_door_open_requires_valid_floor(self) -> None:
        self.system.states["E1"].floor = 99
        self.assertFalse(self.system.control_door("E1", "open"))

    def test_rescue_car_requires_admin_and_empty_confirmation(self) -> None:
        with self.assertRaises(PermissionError):
            self.system.dispatch_rescue_car(
                "tech-card", "E5", 4, occupancy_confirmed_empty=True
            )
        with self.assertRaises(ValueError):
            self.system.dispatch_rescue_car(
                "admin-card", "E5", 4, occupancy_confirmed_empty=False
            )
        selected = self.system.dispatch_rescue_car(
            "admin-card", "E5", 4, occupancy_confirmed_empty=True
        )
        self.assertNotEqual(selected, "E5")

    def test_newer_state_wins_and_older_update_is_rejected(self) -> None:
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertTrue(
            self.system.update_elevator_state(
                ElevatorState("E1", 2, updated_at=now), now
            )
        )
        self.assertFalse(
            self.system.update_elevator_state(
                ElevatorState("E1", 3, updated_at=now - timedelta(seconds=1)), now
            )
        )
        self.assertEqual(self.system.states["E1"].floor, 2)
