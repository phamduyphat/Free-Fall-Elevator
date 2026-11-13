from __future__ import annotations

from analytics.enums import ELEVATOR_IDS, TrafficProfile
from analytics.fleet_metrics import FleetMetrics
from analytics.helpers import (
    calculate_percentile,
    normalize_timestamp,
    summarize_distribution,
)
from analytics.reports import (
    CarUtilizationReport,
    DowntimeReport,
    SecuritySummaryReport,
    TravelTimeReport,
    TripDistributionReport,
    WaitTimeReport,
)
from analytics.security_metrics import SecurityMetrics
from analytics.traffic_metrics import TrafficMetrics


class Metrics:
    """
    Unified `Analytics.Metrics` facade (`analytics/metrics.py`) exposing all 6 KPI
    methods across `TrafficMetrics`, `FleetMetrics`, and `SecurityMetrics`.
    """

    ELEVATOR_IDS: list[str] = ELEVATOR_IDS

    get_wait_time_kpis = staticmethod(TrafficMetrics.get_wait_time_kpis)
    get_travel_time_kpis = staticmethod(TrafficMetrics.get_travel_time_kpis)
    get_trip_distribution = staticmethod(TrafficMetrics.get_trip_distribution)
    get_utilization_kpis = staticmethod(FleetMetrics.get_utilization_kpis)
    get_downtime_summary = staticmethod(FleetMetrics.get_downtime_summary)
    get_security_kpis = staticmethod(SecurityMetrics.get_security_kpis)


__all__ = [
    "CarUtilizationReport",
    "DowntimeReport",
    "ELEVATOR_IDS",
    "FleetMetrics",
    "Metrics",
    "SecurityMetrics",
    "SecuritySummaryReport",
    "TrafficMetrics",
    "TrafficProfile",
    "TravelTimeReport",
    "TripDistributionReport",
    "WaitTimeReport",
    "calculate_percentile",
    "normalize_timestamp",
    "summarize_distribution",
]
