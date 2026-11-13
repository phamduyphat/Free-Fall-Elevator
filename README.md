# Secure and Reliable Elevator Monitoring and Control System

SREMS is a Python implementation of a secure residential elevator monitoring
and control system. The project includes:

- a FastAPI application entry point;
- an asynchronous PostgreSQL connection pool;
- access-control and credential-policy logic for elevators E1-E5;
- tamper-evident audit logging and security-alert tracking; and
- on-demand analytics and CSV/JSON report export.

The repository is also known as **Free-Fall-Elevator**.

## Requirements

- Python 3.11 or newer
- PostgreSQL with the `pgcrypto` extension
- `asyncpg`, `fastapi`, and `uvicorn`

The repository does not currently include a dependency manifest. Install the
runtime dependencies in your environment before starting the application:

```powershell
python -m pip install fastapi uvicorn asyncpg
```

## Database setup

`seed.sql` creates the `srems` database, application role, tables, indexes,
and initial administrator records. Run it from a PostgreSQL installation with
permission to create roles and databases:

```powershell
psql -U postgres -f seed.sql
```

The default connection string is:

```text
postgresql://srems_user:srems_password@localhost:5432/srems
```

To use another database, set `DATABASE_URL` before starting the application:

```powershell
$env:DATABASE_URL = "postgresql://user:password@localhost:5432/database"
```

The credentials in `seed.sql` are development defaults. Change them before
using the system outside a local development environment.

## Run the application

Start the FastAPI application with:

```powershell
python main.py
```

The application listens on `http://127.0.0.1:8000`. FastAPI's interactive
documentation is available at `/docs` when routes are registered.

The application initializes the shared `DbConnectionPool` during startup and
closes it during shutdown. The pool can also be used directly by other
modules through `DbConnectionPool.acquire()` and
`DbConnectionPool.transaction()`.

## Project structure

```text
.
├── main.py                              FastAPI application and lifecycle hooks
├── db.py                                Async PostgreSQL pool and DB dependencies
├── seed.sql                             PostgreSQL schema, indexes, and seed data
├── acs/
│   ├── __init__.py                      ACS namespace facade and exports
│   ├── accesscontrol.py                 Re-exports of access-control classes
│   ├── cab_accesscontrol.py             In-cabin swipe, floor, and E5 freight checks
│   ├── dashboard_accesscontrol.py       Dashboard login, sessions, and audit checks
│   ├── credential_accesscontrol.py      Credential & visitor pass lifecycle management
│   ├── fleet_accesscontrol.py           Maintenance, emergency override, rescue dispatch
│   ├── enums.py                         ACS enums and role/permission mappings
│   ├── payloads.py                      Strongly typed request payloads and results
│   ├── helpers.py                       Shared validation and anomaly-tracking helpers
│   ├── logs.py                          Audit chain, authorization events, alerts
│   └── design_decisions_ACS.md          ACS design and requirements
└── analytics/
    ├── __init__.py                      Analytics namespace facade and exports
    ├── metrics.py                       Unified Metrics facade
    ├── traffic_metrics.py               Wait-time, travel-time, and trip-distribution KPIs
    ├── fleet_metrics.py                 Per-car utilization and downtime/availability KPIs
    ├── security_metrics.py              Security denials, alerts, and incident KPIs
    ├── enums.py                         TrafficProfile enum and elevator constants
    ├── reports.py                       Report and export payload dataclasses
    ├── helpers.py                       Timestamp and percentile/distribution helpers
    ├── export_engine.py                 CSV/JSON report generation
    └── design_decisions_analytics.md    Analytics design and requirements
```

## Using the `acs` and `analytics` modules

### 1. Install packages, seed PostgreSQL, and initialize the pool

```bash
# 1. Install runtime dependencies
python -m pip install fastapi uvicorn asyncpg

# 2. Create the `srems` role, database, schema, and initial Admin card (`ADMIN-CARD-0001`)
psql -U postgres -f seed.sql

# 3. (Optional) Override the default DSN (`postgresql://srems_user:srems_password@localhost:5432/srems`)
export DATABASE_URL="postgresql://srems_user:srems_password@localhost:5432/srems"
```

All `acs` and `analytics` methods are `async` static methods that use `DbConnectionPool` by default:

```python
import asyncio
from datetime import datetime, timezone
from db import DbConnectionPool

async def main() -> None:
    await DbConnectionPool.init_pool()  # Uses DATABASE_URL or default DSN
    try:
        # Optional: reset tables & re-seed `ADMIN-CARD-0001` (useful in tests)
        await DbConnectionPool.reset_database_tables()
        ...
    finally:
        await DbConnectionPool.close_pool()

asyncio.run(main())
```

### 2. Calling `acs` public methods

Import from `acs` (or access via the `ACS` facade: `ACS.CabAccessControl`, `ACS.DashboardAccessControl`, `ACS.CredentialAccessControl`, `ACS.FleetAccessControl`, `ACS.Logging`):

```python
from datetime import datetime, timezone
from acs import (
    CabAccessControl,
    CredentialAccessControl,
    DashboardAccessControl,
    FleetAccessControl,
    Logging,
    CardSwipePayload,
    CarDestinationRequestPayload,
    CredentialAction,
    CredentialManagementPayload,
    DashboardOperationPayload,
    DashboardOperationType,
    E5ServiceRunPayload,
    EmergencyOverridePayload,
    EmergencyOverrideType,
    MaintenanceOperationPayload,
    MaintenanceOperationType,
    RescueCarDispatchPayload,
    ReturnToServicePayload,
    RevokeAndReissuePayload,
    UserRole,
)

now = datetime.now(timezone.utc)

# --- A. Credential lifecycle (`CredentialAccessControl`) ---
# Issue a resident card using the seeded Admin card (`ADMIN-CARD-0001`)
issue_res = await CredentialAccessControl.manage_credential(
    CredentialManagementPayload(
        admin_credential_id="ADMIN-CARD-0001",
        action=CredentialAction.ISSUE,
        target_credential_id="RES-CARD-101",
        payload_details={"role": UserRole.RESIDENT, "assigned_floor": 14, "allowed_floors": [1, 14]},
        timestamp=now,
    )
)
# Also supports: CredentialAction.ISSUE_VISITOR_PASS, SUSPEND, REINSTATE, REVOKE

# Atomic lost-card replacement
replace_res = await CredentialAccessControl.replace_lost_card(
    RevokeAndReissuePayload(
        admin_credential_id="ADMIN-CARD-0001",
        lost_credential_id="RES-CARD-101",
        replacement_credential_id="RES-CARD-102",
        timestamp=now,
    )
)

# --- B. In-cabin access control (`CabAccessControl`) ---
# 1. Tap card on in-cabin reader (opens 5s resident/visitor or 30s staff/tech window)
swipe_res = await CabAccessControl.validate_card_swipe(
    CardSwipePayload(elevator_id="E1", credential_id="RES-CARD-102", origin_floor=1, timestamp=now)
)

# 2. Press destination floor button (consumes single-use car session token; B1 is exempt on E1-E4)
dest_res = await CabAccessControl.validate_car_destination(
    CarDestinationRequestPayload(elevator_id="E1", origin_floor=1, requested_floor=14, timestamp=now)
)

# 3. Initiate dedicated E5 freight run (requires active BUILDING_SERVICE, TECHNICIAN, or ADMIN swipe in E5)
e5_res = await CabAccessControl.validate_e5_service_run(
    E5ServiceRunPayload(elevator_id="E5", destination_floor=10, purpose="FREIGHT", timestamp=now)
)

# --- C. Dashboard sessions & audit integrity (`DashboardAccessControl`) ---
# 1. Card-based dashboard login (creates 15-min sliding-TTL session for ADMIN / TECHNICIAN)
login_res = await DashboardAccessControl.validate_card_swipe(
    CardSwipePayload(credential_id="ADMIN-CARD-0001", is_dashboard_access=True, timestamp=now)
)
token = login_res.session_token

# 2. Validate a privileged dashboard operation (extends session TTL)
op_res = await DashboardAccessControl.validate_dashboard_operation(
    DashboardOperationPayload(
        session_token=token,
        op_type=DashboardOperationType.MANAGE_CREDENTIALS,
        timestamp=now,
    )
)

# 3. Verify SHA-256 audit log chain integrity
auth_res, check_res = await DashboardAccessControl.run_audit_integrity_check(
    admin_credential_id="ADMIN-CARD-0001", timestamp=now
)

# 4. Explicit dashboard logout
await DashboardAccessControl.logout_dashboard_session(session_token=token, timestamp=now)

# --- D. Fleet maintenance & emergency overrides (`FleetAccessControl`) ---
maint_res = await FleetAccessControl.validate_maintenance_operation(
    MaintenanceOperationPayload(
        credential_id="ADMIN-CARD-0001",
        elevator_id="E2",
        operation_type=MaintenanceOperationType.ENTER_MAINTENANCE,
        timestamp=now,
    )
)
rts_res = await FleetAccessControl.validate_return_to_service(
    ReturnToServicePayload(
        credential_id="ADMIN-CARD-0001",
        elevator_id="E2",
        technician_confirmation=True,
        timestamp=now,
    )
)
override_res = await FleetAccessControl.validate_emergency_override(
    EmergencyOverridePayload(
        credential_id="ADMIN-CARD-0001",
        override_type=EmergencyOverrideType.MANUAL_RECALL,
        target_car_id="E3",
        incident_id="INC-001",
        reason="Smoke detector triggered",
        timestamp=now,
    )
)
rescue_res = await FleetAccessControl.validate_rescue_car_dispatch(
    RescueCarDispatchPayload(
        credential_id="ADMIN-CARD-0001",
        incident_id="INC-001",
        rescue_car_id="E4",
        incident_floor=12,
        dwell_wait_seconds=120,
        timestamp=now,
    )
)

# --- E. Direct audit & security log queries (`Logging`) ---
events = await Logging.search_security_logs(elevator_id="E1", outcome="DENIED", limit=50)
alerts = await Logging.get_security_alerts(status="OPEN")
```

### 3. Calling `analytics` public methods

Import `Metrics` (or the domain-specific `TrafficMetrics`, `FleetMetrics`, `SecurityMetrics`) and `ExportEngine` from `analytics`:

```python
from datetime import datetime, timedelta, timezone
from analytics import (
    ExportEngine,
    FleetMetrics,
    Metrics,
    SecurityMetrics,
    TrafficMetrics,
    TrafficProfile,
)

end_ts = datetime.now(timezone.utc)
start_ts = end_ts - timedelta(hours=24)

# 1. Query KPI reports via the unified `Metrics` facade (or domain classes directly)
wait_report = await Metrics.get_wait_time_kpis(start_ts, end_ts, traffic_profile=TrafficProfile.PEAK)
travel_report = await Metrics.get_travel_time_kpis(start_ts, end_ts, elevator_id="E1")
trip_dist = await Metrics.get_trip_distribution(start_ts, end_ts)
utilization = await Metrics.get_utilization_kpis(start_ts, end_ts)       # dict[elevator_id, CarUtilizationReport]
downtime = await Metrics.get_downtime_summary(start_ts, end_ts)
security = await Metrics.get_security_kpis(start_ts, end_ts)

# Each report dataclass provides `.to_dict()`
print(wait_report.to_dict())

# 2. Export a full run report as JSON or CSV (logs `REPORT_EXPORTED` when `admin_id` is provided)
export_payload = await ExportEngine.export_run_report(
    run_id="RUN-2026-01",
    export_format="JSON",          # "JSON" or "CSV"
    include_per_request=True,
    start_time=start_ts,
    end_time=end_ts,
    admin_id="ADMIN-CARD-0001",
)
print(export_payload.content)
```

## Architecture notes

- **Access control is fail-closed:** restricted operations are denied when
  required database checks cannot be completed.
- **Audit records are append-only and hash chained:** each record includes the
  previous record's hash and a SHA-256 current hash.
- **Analytics is read-only:** metrics query operational records and do not
  dispatch elevators or mutate operational state.
- **Simulation timestamps are separate from CPU wall-clock timing:** analytics
  uses persisted timestamps for operational measurements, while wall-clock
  timing is reserved for software-performance checks.
- **E5 is the dedicated service/freight elevator:** residential elevators are
  E1-E4, and E5 has distinct service-run rules.

For detailed requirements and invariants, see
[`acs/design_decisions_ACS.md`](acs/design_decisions_ACS.md) and
[`analytics/design_decisions_analytics.md`](analytics/design_decisions_analytics.md).

## Development

Run Python modules from the repository root so imports such as `from db import
DbConnectionPool` resolve correctly. Before opening a change, verify the
application imports and starts against a configured PostgreSQL instance:

```powershell
python -m compileall main.py db.py acs analytics
python main.py
```

Stop the server with `Ctrl+C`.

## License

No license file is currently included in this repository.
