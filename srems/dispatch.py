"""Eligibility checks and direction-aware elevator dispatch."""

from .config import SREMSConfig
from .models import Availability, Direction, ElevatorState, RequestType


def is_eligible(
    state: ElevatorState, request_type: RequestType, floor: int, config: SREMSConfig
) -> bool:
    elevator = next(
        (item for item in config.elevators if item.elevator_id == state.elevator_id),
        None,
    )
    if (
        elevator is None
        or state.stale
        or state.availability != Availability.IN_SERVICE
        or state.fault is not None
        or state.emergency is not None
    ):
        return False
    if request_type == RequestType.FREIGHT:
        return elevator.freight and floor != -1
    return (
        not elevator.freight
        and (floor != -1 or elevator.serves_b1)
        and request_type != RequestType.SERVICE
    )


def predicted_service_seconds(
    state: ElevatorState,
    target_floor: int,
    config: SREMSConfig,
) -> float:
    distance = abs(state.floor - target_floor)
    dwell_stops = 0
    if state.direction == Direction.IDLE or not state.moving:
        route_distance = distance
    else:
        sign = 1 if state.direction == Direction.UP else -1
        ahead = [
            stop
            for stop in state.pending_stops
            if (stop - state.floor) * sign > 0
        ]
        target_ahead = (target_floor - state.floor) * sign >= 0
        nearest_commitment = (
            min(ahead, key=lambda stop: abs(stop - state.floor))
            if ahead
            else state.floor
        )
        target_before_commitment = target_ahead and (
            (target_floor - state.floor) * sign
            <= (nearest_commitment - state.floor) * sign
        )
        if target_before_commitment:
            route_distance = distance
        else:
            frontier = (
                max(ahead) if sign > 0 else min(ahead)
            ) if ahead else state.floor
            route_distance = abs(frontier - state.floor) + abs(
                target_floor - frontier
            )
            if not target_ahead:
                route_distance += config.stop_dwell_seconds / config.seconds_per_floor
        dwell_stops = sum(
            1
            for stop in state.pending_stops
            if min(state.floor, target_floor) <= stop <= max(state.floor, target_floor)
        )
    return route_distance * config.seconds_per_floor + dwell_stops * config.stop_dwell_seconds


def choose_elevator(
    states: dict[str, ElevatorState],
    floor: int,
    request_type: RequestType,
    config: SREMSConfig,
) -> str | None:
    candidates = [
        state
        for state in states.values()
        if is_eligible(state, request_type, floor, config)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda state: (
            predicted_service_seconds(state, floor, config),
            state.elevator_id,
        ),
    ).elevator_id
