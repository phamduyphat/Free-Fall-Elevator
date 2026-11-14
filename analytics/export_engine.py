from __future__ import annotations

import csv
from datetime import datetime
import io
import json
from typing import Any, Literal, Optional

from acs.logs import AuditActionType, Logging
from acs.payloads import AuditEntry
from analytics.helpers import normalize_timestamp
from analytics.metrics import Metrics
from analytics.reports import ExportPayload
from db import DbConnectionPool
from simulation.operational import operational_simulator


class ExportEngine:
    """
    Analytics.ExportEngine Submodule (`analytics/export_engine.py`).
    Serializes benchmark or historical run results into standard downloadable formats
    (CSV / JSON) and emits `REPORT_EXPORTED` audit events to `ACS.Logging`.
    """

    @staticmethod
    async def export_run_report(
        run_id: str,
        export_format: Literal["CSV", "JSON"] = "JSON",
        include_per_request: bool = True,
        *,
        start_time: Optional[datetime | str] = None,
        end_time: Optional[datetime | str] = None,
        admin_id: Optional[str] = None,
        admin_role: str = "ADMIN",
        current_time: Optional[datetime | str] = None,
        db_pool: type[DbConnectionPool] = DbConnectionPool,
    ) -> ExportPayload:
        """
        Export system performance metrics and optional per-request datasets (ANA-5.9, VT-29).
        Emits a `REPORT_EXPORTED` event to the cryptographic audit ledger.
        """
        gen_time = (
            normalize_timestamp(current_time)
            if current_time is not None
            else datetime.now()
        )

        # Resolve time range bounds if not explicitly supplied
        if start_time is None or end_time is None:
            candidate_timestamps: list[datetime] = []
            with operational_simulator._lock:
                for h in operational_simulator.hall_calls.values():
                    candidate_timestamps.append(normalize_timestamp(h.accepted_at))
                    if h.arrived_at is not None:
                        candidate_timestamps.append(normalize_timestamp(h.arrived_at))
                for t in operational_simulator.trips:
                    candidate_timestamps.append(normalize_timestamp(t["departure_time"]))
                    candidate_timestamps.append(normalize_timestamp(t["arrival_time"]))
            if candidate_timestamps:
                st = normalize_timestamp(start_time or min(candidate_timestamps))
                et = normalize_timestamp(end_time or max(candidate_timestamps))
            else:
                st = normalize_timestamp(start_time or datetime(2026, 1, 1, 0, 0, 0))
                et = normalize_timestamp(end_time or gen_time)
        else:
            st = normalize_timestamp(start_time)
            et = normalize_timestamp(end_time)

        # Aggregate Summary Metrics via Analytics.Metrics
        wait_times = await Metrics.get_wait_time_kpis(st, et, db_pool=db_pool)
        travel_times = await Metrics.get_travel_time_kpis(st, et, db_pool=db_pool)
        trip_dist = await Metrics.get_trip_distribution(st, et, db_pool=db_pool)
        utilization = await Metrics.get_utilization_kpis(st, et, db_pool=db_pool)
        downtime = await Metrics.get_downtime_summary(st, et, report_time=gen_time, db_pool=db_pool)
        security = await Metrics.get_security_kpis(st, et, db_pool=db_pool)

        summary: dict[str, Any] = {
            "run_id": run_id,
            "time_window": {
                "start_time": st.isoformat(),
                "end_time": et.isoformat(),
            },
            "wait_time_kpis": wait_times.to_dict(),
            "travel_time_kpis": travel_times.to_dict(),
            "trip_distribution": trip_dist.to_dict(),
            "car_utilization": {k: v.to_dict() for k, v in utilization.items()},
            "downtime_and_availability": downtime.to_dict(),
            "security_summary": security.to_dict(),
        }

        # Query Per-Request Dataset if requested
        per_request_rows: list[dict[str, Any]] = []
        if include_per_request:
            hall_rows: list[dict[str, Any]] = []
            car_rows: list[dict[str, Any]] = []
            with operational_simulator._lock:
                for h in operational_simulator.hall_calls.values():
                    h_ts = normalize_timestamp(h.accepted_at)
                    if st <= h_ts <= et:
                        b_ts = normalize_timestamp(h.arrived_at) if h.arrived_at is not None else None
                        hall_rows.append(
                            {
                                "call_id": h.request_id,
                                "origin": h.origin_floor,
                                "dest": None,
                                "created_at": h_ts,
                                "arrived_at": b_ts,
                                "completed_at": b_ts,
                                "wait_seconds": h.wait_seconds,
                                "status": h.status,
                                "reassignments": h.reassignment_count,
                            }
                        )
                hall_rows.sort(key=lambda r: r["created_at"])

                for c in operational_simulator.car_calls.values():
                    c_ts = normalize_timestamp(c.accepted_at)
                    if st <= c_ts <= et:
                        comp_ts = normalize_timestamp(c.completed_at) if c.completed_at is not None else None
                        car_rows.append(
                            {
                                "call_id": c.request_id,
                                "origin": c.origin_floor,
                                "dest": c.destination_floor,
                                "created_at": c_ts,
                                "arrived_at": c_ts,
                                "completed_at": comp_ts,
                                "wait_seconds": None,
                                "status": c.status,
                                "reassignments": 0,
                            }
                        )
                car_rows.sort(key=lambda r: r["created_at"])

            for r in hall_rows:
                created_ts = r.get("created_at")
                arrived_ts = r.get("arrived_at")
                completed_ts = r.get("completed_at")
                per_request_rows.append(
                    {
                        "call_id": str(r.get("call_id")),
                        "type": "HALL",
                        "origin": r.get("origin"),
                        "dest": r.get("dest"),
                        "created_at": created_ts.isoformat() if hasattr(created_ts, "isoformat") else created_ts,
                        "arrived_at": arrived_ts.isoformat() if hasattr(arrived_ts, "isoformat") else arrived_ts,
                        "completed_at": completed_ts.isoformat() if hasattr(completed_ts, "isoformat") else completed_ts,
                        "wait_seconds": float(r["wait_seconds"]) if r.get("wait_seconds") is not None else None,
                        "status": str(r.get("status")),
                        "reassignments": int(r.get("reassignments") or 0),
                    }
                )

            for r in car_rows:
                created_ts = r.get("created_at")
                arrived_ts = r.get("arrived_at") or r.get("command_sent_at")
                completed_ts = r.get("completed_at")
                per_request_rows.append(
                    {
                        "call_id": str(r.get("call_id")),
                        "type": "CAR",
                        "origin": r.get("origin"),
                        "dest": r.get("dest"),
                        "created_at": created_ts.isoformat() if hasattr(created_ts, "isoformat") else created_ts,
                        "arrived_at": arrived_ts.isoformat() if hasattr(arrived_ts, "isoformat") else arrived_ts,
                        "completed_at": completed_ts.isoformat() if hasattr(completed_ts, "isoformat") else completed_ts,
                        "wait_seconds": None,
                        "status": str(r.get("status")),
                        "reassignments": 0,
                    }
                )

            # Sort combined per-request entries deterministically by created_at
            per_request_rows.sort(key=lambda x: x["created_at"] or "")

        record_count = len(per_request_rows) if include_per_request else 1

        # Format Content (CSV / JSON)
        fmt_upper = export_format.upper()
        if fmt_upper == "JSON":
            doc = {
                "run_id": run_id,
                "export_format": "JSON",
                "generated_at": gen_time.isoformat(),
                "record_count": record_count,
                "summary": summary,
            }
            if include_per_request:
                doc["per_request_data"] = per_request_rows
            content = json.dumps(doc, indent=2, default=str)
        else:
            # CSV serialization
            output = io.StringIO()
            writer = csv.writer(output)

            # Tabular Per-Request Dataset
            fields = [
                "call_id",
                "type",
                "origin",
                "dest",
                "created_at",
                "arrived_at",
                "completed_at",
                "wait_seconds",
                "status",
                "reassignments",
            ]
            writer.writerow(fields)
            for row in per_request_rows:
                writer.writerow([row.get(f) for f in fields])
            content = output.getvalue()

        # Emit REPORT_EXPORTED audit event when an admin actor is supplied (ANA-5.9, VT-29)
        if admin_id is not None:
            await Logging.write_audit(
                [
                    AuditEntry(
                        timestamp=gen_time,
                        actor_id=admin_id,
                        actor_role=admin_role,
                        action_type=AuditActionType.REPORT_EXPORTED,
                        target_id=run_id,
                        event_payload={
                            "run_id": run_id,
                            "report_type": "PERFORMANCE_AND_AUDIT",
                            "export_format": fmt_upper,
                            "row_count": record_count,
                            "include_per_request": include_per_request,
                            "date_range": {
                                "start_time": st.isoformat(),
                                "end_time": et.isoformat(),
                            },
                        },
                    )
                ],
                db_pool=db_pool,
            )

        return ExportPayload(
            run_id=run_id,
            export_format="CSV" if fmt_upper == "CSV" else "JSON",
            generated_at=gen_time.isoformat(),
            record_count=record_count,
            content=content,
            summary=summary,
            per_request_data=per_request_rows if include_per_request else None,
        )
