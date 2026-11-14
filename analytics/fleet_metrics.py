from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Optional

from analytics.enums import ELEVATOR_IDS
from analytics.helpers import normalize_timestamp
from analytics.reports import CarUtilizationReport, DowntimeReport
from db import DbConnectionPool
from simulation.operational import operational_simulator


class FleetMetrics:
    """
    Analytics.FleetMetrics submodule (`analytics/fleet_metrics.py`).
    Computes per-car utilization/wear metrics and fleet/host downtime and
    availability summaries.
    """

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

        trip_rows: list[dict[str, Any]] = []
        downtime_rows: list[dict[str, Any]] = []
        with operational_simulator._lock:
            for t in operational_simulator.trips:
                dep = normalize_timestamp(t["departure_time"])
                if st <= dep <= et:
                    trip_rows.append(dict(t))
            for d in operational_simulator.downtime_intervals:
                d_start = normalize_timestamp(d["started_at"])
                d_end = normalize_timestamp(d["ended_at"]) if d.get("ended_at") is not None else None
                if d_start <= et and (d_end is None or d_end >= st):
                    row = dict(d)
                    row["started_at"] = d_start
                    row["ended_at"] = d_end
                    downtime_rows.append(row)

        reports: dict[str, CarUtilizationReport] = {
            eid: CarUtilizationReport(elevator_id=eid) for eid in ELEVATOR_IDS
        }

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

        for rep in reports.values():
            non_service_total = (
                rep.fault_seconds
                + rep.maintenance_seconds
                + rep.emergency_seconds
                + rep.occupied_service_seconds
                + rep.stale_seconds
            )
            rep.in_service_seconds = max(0.0, window_seconds - non_service_total)

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

        downtime_rows: list[dict[str, Any]] = []
        heartbeat_rows: list[dict[str, Any]] = []
        with operational_simulator._lock:
            for d in operational_simulator.downtime_intervals:
                d_start = normalize_timestamp(d["started_at"])
                d_end = normalize_timestamp(d["ended_at"]) if d.get("ended_at") is not None else None
                if d_start <= et and (d_end is None or d_end >= st):
                    row = dict(d)
                    row["started_at"] = d_start
                    row["ended_at"] = d_end
                    downtime_rows.append(row)
            downtime_rows.sort(key=lambda r: r["started_at"])

            for hb in operational_simulator.heartbeats:
                hb_ts = normalize_timestamp(hb["timestamp"])
                if st <= hb_ts <= et:
                    heartbeat_rows.append({"timestamp": hb_ts, "status": hb.get("status", "UP")})
            heartbeat_rows.sort(key=lambda r: r["timestamp"])

        async with db_pool.acquire() as conn:
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

        per_car_downtime: dict[str, float] = {eid: 0.0 for eid in ELEVATOR_IDS}
        ongoing_intervals: list[dict[str, Any]] = []

        for r in downtime_rows:
            eid = r["elevator_id"]
            started = r["started_at"]
            ended = r["ended_at"]

            if ended is None:
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
            is_announced = any(
                t_start <= ts <= t_end
                and action == "SYSTEM_SHUTDOWN"
                and (
                    payload.get("is_announced_maintenance") is True
                    or payload.get("reason") == "ANNOUNCED_MAINTENANCE"
                )
                for ts, action, payload in parsed_audits
            )
            if is_announced:
                announced_maint_seconds += gap_dur
            else:
                unannounced_downtime_seconds += gap_dur

        if heartbeat_rows:
            for i in range(len(heartbeat_rows) - 1):
                t1 = heartbeat_rows[i]["timestamp"]
                t2 = heartbeat_rows[i + 1]["timestamp"]
                delta = (t2 - t1).total_seconds()
                if delta > 10.0:
                    classify_gap(t1, t2, delta - 10.0)
        elif total_window_seconds > 10.0:
            classify_gap(st, et, total_window_seconds)

        denom = total_window_seconds - announced_maint_seconds
        if denom <= 0.0:
            availability_pct = 100.0
        else:
            numerator = total_window_seconds - unannounced_downtime_seconds - announced_maint_seconds
            availability_pct = max(0.0, min(100.0, (numerator / denom) * 100.0))

        return DowntimeReport(
            total_downtime_seconds=round(total_car_downtime, 4),
            per_car_downtime_seconds={k: round(v, 4) for k, v in per_car_downtime.items()},
            ongoing_intervals=ongoing_intervals,
            system_availability_pct=round(availability_pct, 4),
            total_window_seconds=round(total_window_seconds, 4),
            unannounced_downtime_seconds=round(unannounced_downtime_seconds, 4),
            announced_maintenance_seconds=round(announced_maint_seconds, 4),
            meets_availability_target=(availability_pct >= 99.5),
        )

