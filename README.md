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

The repository includes a `requirements.txt` file. Install the runtime
dependencies before starting the application:

```powershell
python -m pip install -r requirements.txt
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
python -m uvicorn api:app --reload
```
or via `main.py`:
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
## Mode 1 test-stub integration

The operational simulator, direction-aware dispatch, live stub fleet states, and
backend-connected passenger demo are documented in
[`docs/MODE1_INTEGRATION.md`](docs/MODE1_INTEGRATION.md). This is a **software
test stub** and not a physical elevator controller or safety-certified product.

To run its independent tests without PostgreSQL:

```bash
python -m unittest discover -s tests -p test_operational.py -v
```
