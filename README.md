# Secure and Reliable Elevator Monitoring and Control System

SREMS is a Python implementation of a secure residential elevator monitoring
and control system. The project includes:

- a FastAPI application entry point;
- an asynchronous PostgreSQL connection pool;
- access-control and credential-policy logic for elevators E1-E5;
- tamper-evident audit logging and security-alert tracking; and
- on-demand analytics and CSV/JSON report export.

The repository is also known as **Free-Fall-Elevator**.

## Quick Start (Automated Scripts)

To power on all subsystems with zero friction:

```powershell
# 1. One-time setup: installs dependencies & offers DB seeding
.\setup.ps1

# 2. Power on all subsystems (FastAPI backend + Vite React frontend)
.\start.ps1

# 3. Stop all running subsystems
.\stop.ps1
```

*(Command Prompt / double-click shortcuts: `setup.bat`, `start.bat`, `stop.bat`)*

---

## Requirements

- Python 3.11 or newer
- PostgreSQL with the `pgcrypto` extension
- `asyncpg`, `fastapi`, and `uvicorn`

Install the runtime dependencies from `requirements.txt`:

```powershell
pip install -r requirements.txt
```

### (Optional) Using a virtual environment

To isolate dependencies in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
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
or via uvicorn:
```powershell
python -m uvicorn api:app --reload
```

The application listens on `http://127.0.0.1:8000`. FastAPI's interactive
documentation is available at `/docs` when routes are registered.

To run the visual React frontend (see [frontend/README.md](frontend/README.md)):

```powershell
cd frontend
npm install
npm run dev
```

### Running the simulation

1. Start the backend with `python main.py`.
2. In a separate terminal, start the frontend with `cd frontend; npm run dev`.
3. Open `http://localhost:5173`:
   - **Simulator (stub):** Real-time animated elevator movement for cars `E1`–`E5` across B1 and Floors 1–30, Car Operating Panel (COP), and hall call dispatch.
   - **Wear & Tear:** Live accumulated distance balancing for residential units `E1`–`E4`, 5 configurable balancing strategies, dual trip counters (30-day monthly and 180-day 6-month), and reproducible 6-month synthetic load benchmarks with SVG line graphs.

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

```powershell
python -m unittest discover tests
```
