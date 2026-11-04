# Secure Residential Elevator Management System (SREMS)

SREMS is a Python prototype based on the project requirements in [Group No. 4 SRS.pdf](./Group%20No.%204%20SRS.pdf). It demonstrates elevator request dispatch, credential and floor authorization, emergency and maintenance workflows, and tamper-evident audit logging.

> **Safety and scope:** This is software for simulation and testing. It is not a certified elevator controller and must not be used to operate physical elevators. The included elevator interface is a command-recording test stub; it does not connect to real equipment or guarantee physical safety.

## Requirements

- Python 3.10 or later
- No third-party Python packages

## Run

From the repository root:

```powershell
python -m srems
```

The demo submits an unauthenticated hall call and prints its assignment. It does not control hardware.

## Use the Python API

```python
from srems import Direction, Role, SREMS

system = SREMS()

# Bootstrap is available only until an Admin credential has been created.
system.bootstrap_admin("admin-card", "building-admin")
system.issue_credential(
    actor_id="admin-card",
    credential_id="resident-card",
    user_id="resident-101",
    role=Role.PASSENGER,
    allowed_floors={4, 1},
)

# Hall calls do not require a credential.
hall_request = system.request_hall_call(4, Direction.UP)
print(hall_request.status, hall_request.assigned_elevator)

# Restricted car calls are authorized before a destination command is sent.
car_request = system.submit_car_call(
    elevator_id="E1",
    destination=4,
    credential_id="resident-card",
)
print(car_request.status, car_request.reason)

# B1 is the no-card exception on residential elevators.
b1_request = system.submit_car_call("E1", destination=-1)
```

The building defaults to B1 and floors 1 through 30. E1–E4 serve residential calls; E5 serves freight and service calls and does not serve B1. Building and policy defaults can be customized with `SREMSConfig`.

## Implemented prototype areas

- Request lifecycles, duplicate hall-call merging, direction-aware residential dispatch, and deterministic elevator-ID tie-breaking.
- Simulated credentials, role and floor checks, credential suspension/revocation, visitor passes, and alerts for repeated failures or suspected cloned credentials.
- Emergency incidents, medical responder runs, Admin-only rescue dispatch, maintenance/fault isolation, door-obstruction handling, and communication-loss recovery.
- A SQLite event store and hash-chained audit log with integrity verification.
- Fleet status and wait-time reporting APIs.

Some SRS requirements are conditional on equipment data or a user interface and are not represented as hardware features here. The simulator and in-memory operational state are intended for repeatable prototype tests. SQLite stores event and audit records; it does not currently restore the full live application state after a process restart. See [docs/implementation-plan.md](./docs/implementation-plan.md) and the SRS for scope and requirements.

## Tests

Run the standard-library test suite from the repository root:

```powershell
python -m unittest discover -v
```

Tests cover dispatch, access control, emergency and maintenance operations, communication recovery, and audit integrity.

## Project layout

```text
srems/       Application package, domain models, service, and simulator interface
tests/       Standard-library unit tests
docs/        Implementation plan
```
