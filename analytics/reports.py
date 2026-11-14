from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional


@dataclass
class WaitTimeReport:
    """Statistical summary of hall call wait times (ANA-5.2, FR-1.18, PERF-9, PERF-10)."""

    total_calls: int = 0
    completed_calls: int = 0
    dropped_calls: int = 0
    drop_rate_pct: float = 0.0
    mean_wait_seconds: float = 0.0
    median_wait_seconds: float = 0.0
    p90_wait_seconds: float = 0.0
    p95_wait_seconds: float = 0.0
    min_wait_seconds: float = 0.0
    max_wait_seconds: float = 0.0
    reassigned_calls_count: int = 0
    no_boarding_count: int = 0
    target_met: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TravelTimeReport:
    """Statistical summary of elevator physical cab movement legs (ANA-5.3, FR-1.19)."""

    total_trips: int = 0
    mean_travel_seconds: float = 0.0
    median_travel_seconds: float = 0.0
    p90_travel_seconds: float = 0.0
    p95_travel_seconds: float = 0.0
    min_travel_seconds: float = 0.0
    max_travel_seconds: float = 0.0
    estimated_trips_count: int = 0
    service_runs_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TripDistributionReport:
    """Partitioning of physical motion legs into directional shares (OR-10, BEN-1, BEN-4)."""

    total_trips: int = 0
    up_trips_count: int = 0
    up_trips_share_pct: float = 0.0
    down_non_b1_trips_count: int = 0
    down_non_b1_trips_share_pct: float = 0.0
    down_to_b1_trips_count: int = 0
    down_to_b1_trips_share_pct: float = 0.0
    service_runs_count: int = 0
    service_runs_share_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarUtilizationReport:
    """Per-car wear, distance, and operational state distribution (ANA-5.4)."""

    elevator_id: str
    trip_count: int = 0
    motor_starts: int = 0
    floors_traversed: int = 0
    distance_meters: float = 0.0
    in_service_seconds: float = 0.0
    fault_seconds: float = 0.0
    maintenance_seconds: float = 0.0
    emergency_seconds: float = 0.0
    occupied_service_seconds: float = 0.0
    stale_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DowntimeReport:
    """Elevator downtime and SREMS host availability report (ANA-5.5, AVL-1, VT-26)."""

    total_downtime_seconds: float = 0.0
    per_car_downtime_seconds: dict[str, float] = field(default_factory=dict)
    ongoing_intervals: list[dict[str, Any]] = field(default_factory=list)
    system_availability_pct: float = 100.0
    total_window_seconds: float = 0.0
    unannounced_downtime_seconds: float = 0.0
    announced_maintenance_seconds: float = 0.0
    meets_availability_target: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecuritySummaryReport:
    """Aggregated security anomalies, denials, and alerts summary (ANA-5.6)."""

    total_denials: int = 0
    denials_by_reason: dict[str, int] = field(default_factory=dict)
    failed_auth_attempts: int = 0
    repeated_failure_alerts: int = 0
    cloned_credential_alerts: int = 0
    card_scanning_alerts: int = 0
    door_obstruction_alerts: int = 0
    external_validation_failures: int = 0
    emergency_incidents_by_type: dict[str, int] = field(default_factory=dict)
    total_incidents: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExportPayload:
    """Standardized serialized report container (ANA-5.9, BEN-4, VT-29)."""

    run_id: str
    export_format: Literal["CSV", "JSON"]
    generated_at: str
    record_count: int
    content: str
    summary: dict[str, Any]
    per_request_data: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

