"""Building and SREMS policy configuration."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ElevatorConfig:
    elevator_id: str
    freight: bool = False
    serves_b1: bool = True
    capacity_kg: int = 1000


@dataclass
class SREMSConfig:
    minimum_floor: int = -1
    maximum_floor: int = 30
    common_floors: set[int] = field(default_factory=lambda: {-1, 1})
    responder_floor: int = 1
    maximum_active_requests: int = 100
    failed_authentication_threshold: int = 3
    failed_authentication_window_seconds: int = 60
    cloned_card_window_seconds: int = 10
    unregistered_card_threshold: int = 10
    unregistered_card_window_seconds: int = 300
    repeat_emergency_threshold: int = 4
    repeat_emergency_window_seconds: int = 60
    obstruction_warning_seconds: int = 10
    empty_call_window_seconds: int = 30
    no_boarding_threshold: int = 5
    no_boarding_window_seconds: int = 600
    lost_card_review_hours: int = 72
    visitor_pass_hours: int = 4
    dashboard_session_timeout_seconds: int = 900
    overload_ratio: float = 1.10
    seconds_per_floor: float = 5.0
    stop_dwell_seconds: float = 8.0
    elevators: tuple[ElevatorConfig, ...] = field(
        default_factory=lambda: (
            ElevatorConfig("E1"),
            ElevatorConfig("E2"),
            ElevatorConfig("E3"),
            ElevatorConfig("E4"),
            ElevatorConfig("E5", freight=True, serves_b1=False, capacity_kg=1600),
        )
    )

    def validate_floor(self, floor: int) -> bool:
        return (
            isinstance(floor, int)
            and not isinstance(floor, bool)
            and self.minimum_floor <= floor <= self.maximum_floor
        )
