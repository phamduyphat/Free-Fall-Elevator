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
├── main.py                         FastAPI application and lifecycle hooks
├── db.py                           Async PostgreSQL pool and DB dependencies
├── seed.sql                        PostgreSQL schema, indexes, and seed data
├── acs/
│   ├── accesscontrol.py            Credential and elevator access policies
│   ├── logs.py                     Audit chain, authorization events, alerts
│   └── design_decisions_ACS.md    ACS design and requirements
└── analytics/
    ├── metrics.py                  KPI calculations from persisted records
    ├── export_engine.py            CSV/JSON report generation
    └── design_decisions_analytics.md
                                     Analytics design and requirements
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
