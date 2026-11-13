from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from analytics.enums import TrafficProfile
from analytics.helpers import normalize_timestamp, summarize_distribution
from analytics.reports import (
    TravelTimeReport,
    TripDistributionReport,
    WaitTimeReport,
)
from db import DbConnectionPool


class TrafficMetrics:
    """
    Analytics.TrafficMetrics submodule (`analytics/traffic_metrics.py`).
    Computes passenger wait-time KPIs, cab travel-time KPIs, and directional
    trip distribution shares.
    """

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
        mean_wait, median_wait, p90_wait, p95_wait, min_wait, max_wait = (
            summarize_distribution(waits)
        )

        # Target verification (PERF-9, PERF-10)
        target_met = True
        prof_str = str(traffic_profile).upper() if traffic_profile else None
        if prof_str == "PEAK":
            target_met = (mean_wait <= 60.0) and (p95_wait <= 120.0)
        elif prof_str == "OFF_PEAK":
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
        mean_time, median_time, p90_time, p95_time, min_time, max_time = (
            summarize_distribution(travel_times)
        )

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

