from analytics.export_engine import ExportEngine, ExportPayload
from analytics.metrics import (
    CarUtilizationReport,
    DowntimeReport,
    Metrics,
    SecuritySummaryReport,
    TrafficProfile,
    TravelTimeReport,
    TripDistributionReport,
    WaitTimeReport,
)


class Analytics:
    """Namespace facade exposing `Analytics.Metrics` and `Analytics.ExportEngine`."""

    Metrics = Metrics
    ExportEngine = ExportEngine


__all__ = [
    "Analytics",
    "CarUtilizationReport",
    "DowntimeReport",
    "ExportEngine",
    "ExportPayload",
    "Metrics",
    "SecuritySummaryReport",
    "TrafficProfile",
    "TravelTimeReport",
    "TripDistributionReport",
    "WaitTimeReport",
]

