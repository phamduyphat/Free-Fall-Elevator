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
3. Open `http://localhost:5173` and click **"Simulator (stub)"** in the sidebar (or
   **"Scenario player"** to replay the validation tests, see below):
   - **Shaft view:** Real-time animated elevator movement for cars `E1`–`E5` across B1 and Floors 1–30.
   - **Car Operating Panel (COP):** Swipe simulated cards (e.g. `ADMIN-CARD-0001`), select destination floors, and trigger `HELP` or `MEDICAL` emergencies.
   - **Hall panel:** Submit up/down hall calls or dispatch `E5` freight from any floor.

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
├── simulation/
│   ├── operational.py                   Elevator test stub, dispatch (FR-1.4 + WEAR-3)
│   ├── wear.py                          Trip counters, month/6-month windows, usage penalty
│   └── wear_benchmark.py                1-6 month passenger-journey benchmark (VT-33/34)
├── api/wear.py                          Wear-and-tear API
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
## Scenario player (choose a validation test, press play)

`frontend/public/player/` replays 12 validation scenarios (VT-01 … VT-36) in a building
cross-section: cars ease in and out of floors, doors slide, people queue at the landings and
walk in and out, ropes, sheaves and counterweights move with the cars. Beside it, every step
says which SREMS module acted, why (dispatch cost bars with the wear penalty, incident and
availability state machines, recovery checks, committed routes) and which code ran
(`file:line`). Each test's pass criteria tick off live; a test ends with its verdict and a
"Next test" button.

* Open it from the dashboard sidebar (**Scenario player**) or at `http://localhost:5173/player/`.
  It also works offline: double-click `frontend/public/player/index.html`.
* Keys: `Space` play/pause, `←`/`→` previous/next step, `1`–`9` pick a test, `S` test register.
* Design: the player and the dashboard share one drawing-set style (ink on paper, one ink colour
  per module, status always in words as well as colour, UI-5). Font: Fira Sans, bundled in
  `frontend/public/player/fonts` under the SIL Open Font License (`OFL-FiraSans.txt`), so it also
  works offline.
* Every recording comes from the real `simulation/operational.py` (no hand-made animation).
  Regenerate after changing the simulator:

```powershell
python -m simulation.story_recorder        # writes frontend/public/player/stories.js
python -m unittest tests.test_story_recorder
```

  The recorder refuses to write a scenario whose pass criteria are not all met.
* With the backend running, **Re-run on server** records the scenario again through
  `GET /api/v1/operational/stories/{key}` (a private simulator instance, nothing written to
  the database). `GET /api/v1/operational/stories` lists them.

### Dispatch tuning

`python -m simulation.dispatch_benchmark` runs 110 scenarios (7 traffic patterns × 10 seeds, plus
cars lagging on their trip counters) under several dispatch variants on the same people. The
current settings (wear penalty paused while the group is busy, capped at 15 s) cut the average
wait by 17 % and people waiting over a minute by 33 % against the first version, and still keep
E1–E4 within 0.04 % per month over 6 months. Evidence: [`docs/DISPATCH_TUNING.md`](docs/DISPATCH_TUNING.md).

### Your own tests (people, floors, timing, incidents)

**New test** in the player (or key `N`) opens a builder: how many people, from which floors
(lobby, B1, a band of floors, or floors you type), to which floors, when they appear, whether
they carry goods (E5), where each car starts, its trip counter, and incidents (medical, help,
blocked door, maintenance, drive fault) with start time and duration. Six traffic patterns
(morning rush, evening return, lunch, between floors, move-in day, stress test) fill it in.

**Run test** sends it to `POST /api/v1/operational/stories/custom`. The real simulator runs it
three times on exactly the same people (same seed): SREMS dispatch as built, SREMS without wear
balancing, and a nearest-car baseline. The SREMS run plays in the drawing; the right-hand column
shows a comparison table (average, 95th-percentile and longest wait, people waiting over 60 s,
journey time, floors travelled) and plain findings: how much SREMS gains over nearest-car, what
wear balancing costs, which floors waited longest, and whether your targets were met. Failing
criteria are reported, not hidden.

Without the dashboard: **Save test file** in the builder, then

```powershell
python -m simulation.custom_scenario my_test.json            # report in the terminal
python -m simulation.custom_scenario my_test.json --player   # also add it to the player
python -m simulation.custom_scenario --example               # a test file to start from
```

**Full-load bypass (FR-1.14, CAP-2).** Custom scenarios exposed a livelock: a full car left
someone behind, the call was assigned straight back to the same car (it was at that floor), and
it reopened there forever. The simulator now takes load data when there is a source (the test
stub reports people on board over rated capacity): a full car gets no new hall calls and passes
its pickups, which go back to the queue for another car with their acceptance time kept.

## Wear-and-tear balancing (phase-2 NFR, WEAR-1..WEAR-6)

Every car has trip counters (floors travelled, motor starts, door cycles) per calendar month,
per 6-month period (reset every 6 months) and lifetime. Dispatch adds a bounded usage penalty
(at most 15 s, paused while the group is busy) to busier cars, which keeps E1–E4 within **2 % per month** and **5 % per
6 months**. E5 is reported as its own group (it alone serves freight calls, FR-1.13).
Details, evidence and open points: [`docs/WEAR_LEVELING.md`](docs/WEAR_LEVELING.md).

* Dashboard: sign in as Admin/Technician → **Wear & tear**.
* API: `GET /api/v1/operational/wear`, `POST|GET /api/v1/operational/wear/benchmark`,
  `GET /api/v1/operational/wear/report`.
* Offline proof run (no database needed):

```powershell
python -m simulation.wear_benchmark --months 1 --compare
python -m simulation.wear_benchmark --months 6 --compare --outages --out docs/wear_report.json
```

* `SREMS_WEAR_BALANCING=0` reproduces the original dispatcher for an A/B demo.
* **Re-run `seed.sql`** on existing databases: it adds `elevator_wear_counters` and
  `elevator_wear_history` (idempotent; it now also runs cleanly on a fresh database).
* Peak/off-peak labels (PERF-8) use building-local time: `start.ps1` sets
  `SREMS_BUILDING_UTC_OFFSET_HOURS=7` (Hanoi); change it with `-BuildingUtcOffsetHours`.

## Mode 1 test-stub integration

The operational simulator, direction-aware dispatch, live stub fleet states, and
backend-connected passenger demo are documented in
[`docs/MODE1_INTEGRATION.md`](docs/MODE1_INTEGRATION.md). This is a **software
test stub** and not a physical elevator controller or safety-certified product.

To run its independent tests without PostgreSQL:

```powershell
python -m unittest discover tests
```
