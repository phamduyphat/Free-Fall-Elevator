from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import math
import statistics
from typing import Any, Optional

from db import DbConnectionPool


class TrafficProfile(StrEnum):
    PEAK = "PEAK"
    OFF_PEAK = "OFF_PEAK"
    NORMAL = "NORMAL"
    STRESS = "STRESS"


def normalize_timestamp(ts: datetime | str) -> datetime:
    """Normalize a datetime or ISO string to naive UTC datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def calculate_percentile(values: list[float], p: float) -> float:
    """
    Calculate percentile p (0-100) using linear interpolation
    consistent with numpy.percentile and PostgreSQL percentile_cont.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1


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


class Metrics:
    """
    Analytics.Metrics Submodule (`analytics/metrics.py`).
    Provides on-demand SQL aggregation queries and mathematical formulations
    for SREMS Mode 5 without mutating operational state.
    """

    ELEVATOR_IDS: list[str] = ["E1", "E2", "E3", "E4", "E5"]

    @staticmethod
    async def get_wait_time_kpis(
        start_time: datetime | str,
        end_time: datetime | str,
        traffic_profile: Optional[TrafficProfile | str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> WaitTimeReport:
        """
        Compute wait-time KPIs from `hall_calls` over [start_time, end_time] (ANA-5.2, FR-1.18, PERF-9).
        Excludes cancelled/failed/blocked calls from wait distributions and computes drop rate.
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)

        query = """
            SELECT
                call_id,
                status,
                wait_time_seconds,
                reassignments_count,
                no_boarding,
                traffic_profile
            FROM hall_calls
            WHERE created_at >= $1 AND created_at <= $2
        """
        params: list[Any] = [st, et]
        if traffic_profile is not None:
            params.append(str(traffic_profile))
            query += f" AND (traffic_profile IS NULL OR traffic_profile = ${len(params)})"

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        total_calls = len(rows)
        if total_calls == 0:
            return WaitTimeReport()

        completed_rows = [r for r in rows if r["status"] == "COMPLETED"]
        dropped_rows = [r for r in rows if r["status"] in ("CANCELLED", "FAILED", "BLOCKED")]
        reassigned_count = sum(1 for r in rows if (r.get("reassignments_count") or 0) > 0)
        no_boarding_count = sum(1 for r in rows if r.get("no_boarding"))

        completed_calls = len(completed_rows)
        dropped_calls = len(dropped_rows)
        drop_rate_pct = (dropped_calls / total_calls * 100.0) if total_calls > 0 else 0.0

        waits = [
            float(r["wait_time_seconds"])
            for r in completed_rows
            if r["wait_time_seconds"] is not None
        ]

        if waits:
            mean_wait = statistics.fmean(waits)
            median_wait = calculate_percentile(waits, 50.0)
            p90_wait = calculate_percentile(waits, 90.0)
            p95_wait = calculate_percentile(waits, 95.0)
            min_wait = min(waits)
            max_wait = max(waits)
        else:
            mean_wait = 0.0
            median_wait = 0.0
            p90_wait = 0.0
            p95_wait = 0.0
            min_wait = 0.0
            max_wait = 0.0

        # Target verification (PERF-9, PERF-10)
        target_met = True
        prof_str = str(traffic_profile).upper() if traffic_profile else None
        if prof_str == "PEAK":
            # Peak: mean <= 60s, p95 <= 120s
            target_met = (mean_wait <= 60.0) and (p95_wait <= 120.0)
        elif prof_str == "OFF_PEAK":
            # Off-Peak: mean <= 90s
            target_met = mean_wait <= 90.0

        return WaitTimeReport(
            total_calls=total_calls,
            completed_calls=completed_calls,
            dropped_calls=dropped_calls,
            drop_rate_pct=round(drop_rate_pct, 4),
            mean_wait_seconds=round(mean_wait, 4),
            median_wait_seconds=round(median_wait, 4),
            p90_wait_seconds=round(p90_wait, 4),
            p95_wait_seconds=round(p95_wait, 4),
            min_wait_seconds=round(min_wait, 4),
            max_wait_seconds=round(max_wait, 4),
            reassigned_calls_count=reassigned_count,
            no_boarding_count=no_boarding_count,
            target_met=target_met,
        )

    @staticmethod
    async def get_travel_time_kpis(
        start_time: datetime | str,
        end_time: datetime | str,
        elevator_id: Optional[str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> TravelTimeReport:
        """
        Compute elevator physical travel-time KPIs from `trips` over [start_time, end_time] (ANA-5.3).
        Tracks locally estimated timestamps separately from hardware-verified legs.
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)

        query = """
            SELECT
                travel_time_seconds,
                is_estimated,
                is_service_run
            FROM trips
            WHERE departure_time >= $1 AND departure_time <= $2
        """
        params: list[Any] = [st, et]
        if elevator_id is not None:
            params.append(elevator_id)
            query += f" AND elevator_id = ${len(params)}"

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        total_trips = len(rows)
        if total_trips == 0:
            return TravelTimeReport()

        travel_times = [
            float(r["travel_time_seconds"])
            for r in rows
            if r["travel_time_seconds"] is not None
        ]
        estimated_count = sum(1 for r in rows if r.get("is_estimated"))
        service_runs_count = sum(1 for r in rows if r.get("is_service_run"))

        if travel_times:
            mean_time = statistics.fmean(travel_times)
            median_time = calculate_percentile(travel_times, 50.0)
            p90_time = calculate_percentile(travel_times, 90.0)
            p95_time = calculate_percentile(travel_times, 95.0)
            min_time = min(travel_times)
            max_time = max(travel_times)
        else:
            mean_time = 0.0
            median_time = 0.0
            p90_time = 0.0
            p95_time = 0.0
            min_time = 0.0
            max_time = 0.0

        return TravelTimeReport(
            total_trips=total_trips,
            mean_travel_seconds=round(mean_time, 4),
            median_travel_seconds=round(median_time, 4),
            p90_travel_seconds=round(p90_time, 4),
            p95_travel_seconds=round(p95_time, 4),
            min_travel_seconds=round(min_time, 4),
            max_travel_seconds=round(max_time, 4),
            estimated_trips_count=estimated_count,
            service_runs_count=service_runs_count,
        )

    @staticmethod
    async def get_trip_distribution(
        start_time: datetime | str,
        end_time: datetime | str,
        elevator_id: Optional[str] = None,
        traffic_profile: Optional[TrafficProfile | str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> TripDistributionReport:
        """
        Compute trip distribution shares partitioned into Up, Down (non-B1), and Down (to B1)
        as well as dedicated E5 service runs (OR-10, BEN-1, BEN-4).
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)

        query = """
            SELECT
                elevator_id,
                origin_floor,
                destination_floor,
                is_service_run
            FROM trips
            WHERE departure_time >= $1 AND departure_time <= $2
        """
        params: list[Any] = [st, et]
        if elevator_id is not None:
            params.append(elevator_id)
            query += f" AND elevator_id = ${len(params)}"
        if traffic_profile is not None:
            params.append(str(traffic_profile))
            query += f" AND traffic_profile = ${len(params)}"

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        up_count = 0
        down_non_b1_count = 0
        down_to_b1_count = 0
        service_runs_count = 0

        for r in rows:
            orig = int(r["origin_floor"])
            dest = int(r["destination_floor"])
            eid = str(r["elevator_id"])
            is_service = bool(r["is_service_run"])

            if dest > orig:
                up_count += 1
            elif dest < orig:
                if dest == -1:
                    down_to_b1_count += 1
                else:
                    down_non_b1_count += 1

            if eid == "E5" and is_service:
                service_runs_count += 1

        total_trips = up_count + down_non_b1_count + down_to_b1_count
        if total_trips > 0:
            up_share = (up_count / total_trips) * 100.0
            down_non_b1_share = (down_non_b1_count / total_trips) * 100.0
            down_to_b1_share = (down_to_b1_count / total_trips) * 100.0
            service_share = (service_runs_count / total_trips) * 100.0
        else:
            up_share = 0.0
            down_non_b1_share = 0.0
            down_to_b1_share = 0.0
            service_share = 0.0

        return TripDistributionReport(
            total_trips=total_trips,
            up_trips_count=up_count,
            up_trips_share_pct=round(up_share, 4),
            down_non_b1_trips_count=down_non_b1_count,
            down_non_b1_trips_share_pct=round(down_non_b1_share, 4),
            down_to_b1_trips_count=down_to_b1_count,
            down_to_b1_trips_share_pct=round(down_to_b1_share, 4),
            service_runs_count=service_runs_count,
            service_runs_share_pct=round(service_share, 4),
        )

    @staticmethod
    async def get_utilization_kpis(
        start_time: datetime | str,
        end_time: datetime | str,
        floor_height_meters: float = 3.0,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> dict[str, CarUtilizationReport]:
        """
        Compute wear, distance, and operational state distribution per car (ANA-5.4).
        Derives operational state hours from `trips` and `car_downtime_intervals`.
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)
        window_seconds = max(0.0, (et - st).total_seconds())

        async with db_pool.acquire() as conn:
            trip_rows = await conn.fetch(
                """
                SELECT
                    elevator_id,
                    travel_time_seconds,
                    floors_traversed,
                    motor_starts,
                    is_service_run
                FROM trips
                WHERE departure_time >= $1 AND departure_time <= $2
                """,
                st,
                et,
            )

            downtime_rows = await conn.fetch(
                """
                SELECT
                    elevator_id,
                    state,
                    reason,
                    started_at,
                    ended_at,
                    duration_seconds
                FROM car_downtime_intervals
                WHERE started_at <= $2 AND (ended_at IS NULL OR ended_at >= $1)
                """,
                st,
                et,
            )

        reports: dict[str, CarUtilizationReport] = {
            eid: CarUtilizationReport(elevator_id=eid) for eid in Metrics.ELEVATOR_IDS
        }

        # Process trips
        for r in trip_rows:
            eid = r["elevator_id"]
            if eid not in reports:
                reports[eid] = CarUtilizationReport(elevator_id=eid)
            rep = reports[eid]
            rep.trip_count += 1
            rep.motor_starts += int(r.get("motor_starts") or 1)
            traversed = int(r.get("floors_traversed") or 0)
            rep.floors_traversed += traversed
            rep.distance_meters += traversed * floor_height_meters

            if eid == "E5" and r.get("is_service_run"):
                rep.occupied_service_seconds += float(r.get("travel_time_seconds") or 0.0)

        # Process downtime overlaps
        for r in downtime_rows:
            eid = r["elevator_id"]
            if eid not in reports:
                reports[eid] = CarUtilizationReport(elevator_id=eid)
            rep = reports[eid]

            int_start = max(r["started_at"], st)
            int_end = min(r["ended_at"] or et, et)
            overlap = max(0.0, (int_end - int_start).total_seconds())

            state = str(r["state"])
            reason = str(r.get("reason") or "")

            if state == "FAULT":
                rep.fault_seconds += overlap
            elif state == "MAINTENANCE":
                rep.maintenance_seconds += overlap
            elif state == "EMERGENCY":
                rep.emergency_seconds += overlap

            if reason == "COMMUNICATION_LOSS":
                rep.stale_seconds += overlap

        # Compute In-Service time
        for rep in reports.values():
            non_service_total = (
                rep.fault_seconds
                + rep.maintenance_seconds
                + rep.emergency_seconds
                + rep.occupied_service_seconds
                + rep.stale_seconds
            )
            rep.in_service_seconds = max(0.0, window_seconds - non_service_total)

            # Round metrics
            rep.distance_meters = round(rep.distance_meters, 2)
            rep.in_service_seconds = round(rep.in_service_seconds, 4)
            rep.fault_seconds = round(rep.fault_seconds, 4)
            rep.maintenance_seconds = round(rep.maintenance_seconds, 4)
            rep.emergency_seconds = round(rep.emergency_seconds, 4)
            rep.occupied_service_seconds = round(rep.occupied_service_seconds, 4)
            rep.stale_seconds = round(rep.stale_seconds, 4)

        return reports

    @staticmethod
    async def get_downtime_summary(
        start_time: datetime | str,
        end_time: datetime | str,
        report_time: Optional[datetime | str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> DowntimeReport:
        """
        Compute elevator downtime summary and stateless software availability (ANA-5.5, AVL-1, VT-26).
        Evaluates 10-second heartbeat gaps and correlates with restart audit logs.
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)
        rt = normalize_timestamp(report_time) if report_time is not None else et
        total_window_seconds = max(0.0, (et - st).total_seconds())

        async with db_pool.acquire() as conn:
            downtime_rows = await conn.fetch(
                """
                SELECT
                    interval_id,
                    elevator_id,
                    state,
                    reason,
                    started_at,
                    ended_at,
                    duration_seconds,
                    technician_id
                FROM car_downtime_intervals
                WHERE started_at <= $2 AND (ended_at IS NULL OR ended_at >= $1)
                ORDER BY started_at ASC
                """,
                st,
                et,
            )

            heartbeat_rows = await conn.fetch(
                """
                SELECT
                    timestamp,
                    status
                FROM system_heartbeats
                WHERE timestamp >= $1 AND timestamp <= $2
                ORDER BY timestamp ASC
                """,
                st,
                et,
            )

            shutdown_audit_rows = await conn.fetch(
                """
                SELECT
                    timestamp,
                    action_type,
                    event_payload::text AS payload_text
                FROM audit_log_chain
                WHERE timestamp >= $1 AND timestamp <= $2
                  AND action_type IN ('SYSTEM_SHUTDOWN', 'SYSTEM_STARTUP')
                ORDER BY timestamp ASC
                """,
                st,
                et,
            )

        per_car_downtime: dict[str, float] = {eid: 0.0 for eid in Metrics.ELEVATOR_IDS}
        ongoing_intervals: list[dict[str, Any]] = []

        for r in downtime_rows:
            eid = r["elevator_id"]
            started = r["started_at"]
            ended = r["ended_at"]

            if ended is None:
                # Ongoing interval dynamically accumulated up to rt
                elapsed = max(0.0, (rt - started).total_seconds())
                per_car_downtime[eid] = per_car_downtime.get(eid, 0.0) + elapsed
                ongoing_intervals.append(
                    {
                        "interval_id": str(r["interval_id"]),
                        "elevator_id": eid,
                        "state": str(r["state"]),
                        "reason": str(r["reason"]),
                        "started_at": started.isoformat(),
                        "current_duration_seconds": round(elapsed, 2),
                        "technician_id": r["technician_id"],
                        "is_ongoing": True,
                    }
                )
            else:
                dur = float(r["duration_seconds"] or (ended - started).total_seconds())
                per_car_downtime[eid] = per_car_downtime.get(eid, 0.0) + dur

        total_car_downtime = sum(per_car_downtime.values())

        # Software availability analysis via heartbeat gaps (AVL-1)
        unannounced_downtime_seconds = 0.0
        announced_maint_seconds = 0.0

        def parse_shutdown_payload(text: str) -> dict[str, Any]:
            try:
                return json.loads(text)
            except Exception:
                return {}

        parsed_audits = [
            (
                r["timestamp"],
                str(r["action_type"]),
                parse_shutdown_payload(r["payload_text"]),
            )
            for r in shutdown_audit_rows
        ]

        def classify_gap(t_start: datetime, t_end: datetime, gap_dur: float) -> None:
            nonlocal unannounced_downtime_seconds, announced_maint_seconds
            # Inspect audits within gap
            is_announced = False
            for ts, action, payload in parsed_audits:
                if t_start <= ts <= t_end and action == "SYSTEM_SHUTDOWN":
                    if payload.get("is_announced_maintenance") is True or payload.get("reason") == "ANNOUNCED_MAINTENANCE":
                        is_announced = True
                        break
            if is_announced:
                announced_maint_seconds += gap_dur
            else:
                unannounced_downtime_seconds += gap_dur

        if heartbeat_rows:
            # Check gap between consecutive samples
            for i in range(len(heartbeat_rows) - 1):
                t1 = heartbeat_rows[i]["timestamp"]
                t2 = heartbeat_rows[i + 1]["timestamp"]
                delta = (t2 - t1).total_seconds()
                if delta > 10.0:
                    gap_duration = delta - 10.0
                    classify_gap(t1, t2, gap_duration)
        else:
            # No heartbeats recorded across the entire window
            if total_window_seconds > 10.0:
                classify_gap(st, et, total_window_seconds)

        # Compute Availability Percentage (AVL-1)
        denom = total_window_seconds - announced_maint_seconds
        if denom <= 0.0:
            availability_pct = 100.0
        else:
            numerator = total_window_seconds - unannounced_downtime_seconds - announced_maint_seconds
            availability_pct = max(0.0, min(100.0, (numerator / denom) * 100.0))

        meets_target = availability_pct >= 99.5

        return DowntimeReport(
            total_downtime_seconds=round(total_car_downtime, 4),
            per_car_downtime_seconds={k: round(v, 4) for k, v in per_car_downtime.items()},
            ongoing_intervals=ongoing_intervals,
            system_availability_pct=round(availability_pct, 4),
            total_window_seconds=round(total_window_seconds, 4),
            unannounced_downtime_seconds=round(unannounced_downtime_seconds, 4),
            announced_maintenance_seconds=round(announced_maint_seconds, 4),
            meets_availability_target=meets_target,
        )

    @staticmethod
    async def get_security_kpis(
        start_time: datetime | str,
        end_time: datetime | str,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> SecuritySummaryReport:
        """
        Aggregate authorization denials, security alerts, external rejection events,
        and emergency incidents over [start_time, end_time] (ANA-5.6).
        """
        st = normalize_timestamp(start_time)
        et = normalize_timestamp(end_time)

        async with db_pool.acquire() as conn:
            denial_rows = await conn.fetch(
                """
                SELECT denial_reason, count(*) AS cnt
                FROM access_authorization_events
                WHERE timestamp >= $1 AND timestamp <= $2
                  AND outcome = 'DENIED'
                GROUP BY denial_reason
                """,
                st,
                et,
            )

            alert_rows = await conn.fetch(
                """
                SELECT alert_type, count(*) AS cnt
                FROM security_alerts
                WHERE created_at >= $1 AND created_at <= $2
                GROUP BY alert_type
                """,
                st,
                et,
            )

            malformed_rows = await conn.fetch(
                """
                SELECT count(*) AS cnt
                FROM audit_log_chain
                WHERE timestamp >= $1 AND timestamp <= $2
                  AND action_type = 'MALFORMED_MSG_REJECTED'
                """,
                st,
                et,
            )

            incident_rows = await conn.fetch(
                """
                SELECT emergency_type, count(*) AS cnt
                FROM incidents
                WHERE created_at >= $1 AND created_at <= $2
                GROUP BY emergency_type
                """,
                st,
                et,
            )

        denials_by_reason: dict[str, int] = {
            str(r["denial_reason"] or "UNKNOWN"): int(r["cnt"]) for r in denial_rows
        }
        total_denials = sum(denials_by_reason.values())

        alerts_by_type: dict[str, int] = {
            str(r["alert_type"]): int(r["cnt"]) for r in alert_rows
        }
        repeated_failures = alerts_by_type.get("REPEATED_FAILURES", 0)
        cloned_credentials = alerts_by_type.get("CLONED_CREDENTIAL", 0)
        card_scanning = alerts_by_type.get("CARD_SCANNING", 0)
        door_obstructions = alerts_by_type.get("DOOR_OBSTRUCTION", 0)

        external_rejections = int(malformed_rows[0]["cnt"]) if malformed_rows else 0

        incidents_by_type: dict[str, int] = {
            str(r["emergency_type"]): int(r["cnt"]) for r in incident_rows
        }
        total_incidents = sum(incidents_by_type.values())

        # Failed auth attempts includes denials due to unregistered/suspended/expired/revoked cards
        auth_failure_reasons = {
            "UNREGISTERED_CARD",
            "CARD_SUSPENDED",
            "CARD_REVOKED",
            "CARD_EXPIRED",
            "NO_ACTIVE_CREDENTIAL",
        }
        failed_auth = sum(
            count for reason, count in denials_by_reason.items() if reason in auth_failure_reasons
        )

        return SecuritySummaryReport(
            total_denials=total_denials,
            denials_by_reason=denials_by_reason,
            failed_auth_attempts=failed_auth,
            repeated_failure_alerts=repeated_failures,
            cloned_credential_alerts=cloned_credentials,
            card_scanning_alerts=card_scanning,
            door_obstruction_alerts=door_obstructions,
            external_validation_failures=external_rejections,
            emergency_incidents_by_type=incidents_by_type,
            total_incidents=total_incidents,
        )

