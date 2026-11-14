from __future__ import annotations

from datetime import datetime

from analytics.helpers import normalize_timestamp
from analytics.reports import SecuritySummaryReport
from db import DbConnectionPool
from simulation.operational import operational_simulator

AUTH_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "UNREGISTERED_CARD",
        "CARD_SUSPENDED",
        "CARD_REVOKED",
        "CARD_EXPIRED",
        "NO_ACTIVE_CREDENTIAL",
    }
)


class SecurityMetrics:
    """
    Analytics.SecurityMetrics submodule (`analytics/security_metrics.py`).
    Aggregates authorization denials, security alerts, malformed message
    rejections, and emergency incidents.
    """

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

        incidents_by_type: dict[str, int] = {}
        with operational_simulator._lock:
            for inc in operational_simulator.incidents.values():
                inc_ts = normalize_timestamp(inc.created_at)
                if st <= inc_ts <= et:
                    etype = str(inc.kind)
                    incidents_by_type[etype] = incidents_by_type.get(etype, 0) + 1

        denials_by_reason: dict[str, int] = {
            str(r["denial_reason"] or "UNKNOWN"): int(r["cnt"]) for r in denial_rows
        }
        total_denials = sum(denials_by_reason.values())

        alerts_by_type: dict[str, int] = {
            str(r["alert_type"]): int(r["cnt"]) for r in alert_rows
        }
        external_rejections = int(malformed_rows[0]["cnt"]) if malformed_rows else 0

        total_incidents = sum(incidents_by_type.values())

        failed_auth = sum(
            count for reason, count in denials_by_reason.items() if reason in AUTH_FAILURE_REASONS
        )

        return SecuritySummaryReport(
            total_denials=total_denials,
            denials_by_reason=denials_by_reason,
            failed_auth_attempts=failed_auth,
            repeated_failure_alerts=alerts_by_type.get("REPEATED_FAILURES", 0),
            cloned_credential_alerts=alerts_by_type.get("CLONED_CREDENTIAL", 0),
            card_scanning_alerts=alerts_by_type.get("CARD_SCANNING", 0),
            door_obstruction_alerts=alerts_by_type.get("DOOR_OBSTRUCTION", 0),
            external_validation_failures=external_rejections,
            emergency_incidents_by_type=incidents_by_type,
            total_incidents=total_incidents,
        )

