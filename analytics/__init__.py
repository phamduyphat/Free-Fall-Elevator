from analytics.enums import ELEVATOR_IDS, TrafficProfile
from analytics.export_engine import ExportEngine
from analytics.fleet_metrics import FleetMetrics
from analytics.metrics import Metrics
from analytics.reports import (
    CarUtilizationReport,
    DowntimeReport,
    ExportPayload,
    SecuritySummaryReport,
    TravelTimeReport,
    TripDistributionReport,
    WaitTimeReport,
)
from analytics.security_metrics import SecurityMetrics
from analytics.traffic_metrics import TrafficMetrics


class Analytics:
    """Namespace facade exposing `Metrics`, `TrafficMetrics`, `FleetMetrics`, `SecurityMetrics`, and `ExportEngine`."""

    Metrics = Metrics
    TrafficMetrics = TrafficMetrics
    FleetMetrics = FleetMetrics
    SecurityMetrics = SecurityMetrics
    ExportEngine = ExportEngine


__all__ = [
    "Analytics",
    "CarUtilizationReport",
    "DowntimeReport",
    "ELEVATOR_IDS",
    "ExportEngine",
    "ExportPayload",
    "FleetMetrics",
    "Metrics",
    "SecurityMetrics",
    "SecuritySummaryReport",
    "TrafficMetrics",
    "TrafficProfile",
    "TravelTimeReport",
    "TripDistributionReport",
    "WaitTimeReport",
]
