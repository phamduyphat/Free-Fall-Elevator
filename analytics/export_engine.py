from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import io
import json
from typing import Any, Literal, Optional

from acs.logs import AuditActionType, Logging
from analytics.metrics import Metrics, normalize_timestamp
from db import DbConnectionPool


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
            async with db_pool.acquire() as conn:
                bounds = await conn.fetchrow(
                    """
                    SELECT
                        MIN(min_ts) AS start_ts,
                        MAX(max_ts) AS end_ts
                    FROM (
                        SELECT MIN(created_at) AS min_ts, MAX(coalesce(completed_at, created_at)) AS max_ts FROM hall_calls
                        UNION ALL
                        SELECT MIN(departure_time) AS min_ts, MAX(arrival_time) AS max_ts FROM trips
                    ) t
                    """
                )
                if bounds and bounds["start_ts"] and bounds["end_ts"]:
                    st = normalize_timestamp(start_time or bounds["start_ts"])
                    et = normalize_timestamp(end_time or bounds["end_ts"])
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
            async with db_pool.acquire() as conn:
                hall_rows = await conn.fetch(
                    """
                    SELECT
                        call_id,
                        'HALL' AS type,
                        origin_floor AS origin,
                        NULL::int AS dest,
                        created_at,
                        arrived_at,
                        completed_at,
                        wait_time_seconds AS wait_seconds,
                        status,
                        reassignments_count AS reassignments
                    FROM hall_calls
                    WHERE created_at >= $1 AND created_at <= $2
                    ORDER BY created_at ASC
                    """,
                    st,
                    et,
                )

                car_rows = await conn.fetch(
                    """
                    SELECT
                        call_id,
                        'CAR' AS type,
                        NULL::int AS origin,
                        destination_floor AS dest,
                        created_at,
                        command_sent_at AS arrived_at,
                        completed_at,
                        NULL::double precision AS wait_seconds,
                        status,
                        0 AS reassignments
                    FROM car_calls
                    WHERE created_at >= $1 AND created_at <= $2
                    ORDER BY created_at ASC
                    """,
                    st,
                    et,
                )

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
