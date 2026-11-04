"""Dispatch behavior tests."""

import unittest

from srems.config import SREMSConfig
from srems.dispatch import choose_elevator
from srems.models import Availability, Direction, ElevatorState, RequestType


class DispatchTests(unittest.TestCase):
    def test_direction_and_committed_stops_outweigh_nearest_car(self) -> None:
        states = {
            "E1": ElevatorState(
                "E1",
                3,
                direction=Direction.UP,
                moving=True,
                pending_stops=[8],
            ),
            "E2": ElevatorState(
                "E2",
                8,
                direction=Direction.DOWN,
                moving=True,
            ),
            "E3": ElevatorState("E3", 20),
            "E4": ElevatorState("E4", 25),
            "E5": ElevatorState("E5", 1),
        }
        self.assertEqual(
            choose_elevator(states, 2, RequestType.HALL, SREMSConfig()), "E2"
        )

    def test_ties_use_lowest_elevator_id_and_exclude_unavailable(self) -> None:
        states = {
            elevator_id: ElevatorState(elevator_id, 5)
            for elevator_id in ("E1", "E2", "E3", "E4", "E5")
        }
        states["E1"].availability = Availability.MAINTENANCE
        self.assertEqual(
            choose_elevator(states, 8, RequestType.HALL, SREMSConfig()), "E2"
        )

    def test_freight_calls_are_served_only_by_e5_and_not_to_b1(self) -> None:
        states = {
            elevator_id: ElevatorState(elevator_id, 5)
            for elevator_id in ("E1", "E2", "E3", "E4", "E5")
        }
        config = SREMSConfig()
        self.assertEqual(
            choose_elevator(states, 10, RequestType.FREIGHT, config), "E5"
        )
        self.assertIsNone(choose_elevator(states, -1, RequestType.FREIGHT, config))
