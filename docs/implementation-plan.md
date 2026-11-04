# SREMS Python Implementation Plan

## Scope and assumptions

Implement the SREMS prototype described in `Group No. 4 SRS.pdf` as a software-only Python service. Elevator equipment remains behind a logical interface; the included simulator is a test stub, not a safety-certified controller. The first implementation targets the SRS must-have baseline and keeps conditional hardware-dependent behavior explicit.

## Phases

1. **Create the package structure** — separate domain models, configuration, access control, dispatch, audit/persistence, elevator interface, service orchestration, and CLI entry points.
2. **Implement domain and persistence** — model elevators, requests, credentials, incidents, and events; persist operational/audit records with a verifiable hash chain.
3. **Implement passenger and dispatch flows** — validate floors and elevator eligibility; merge duplicate calls; dispatch direction-aware residential requests deterministically; keep freight service isolated to E5; measure waits.
4. **Implement security and operations** — enforce credential lifecycle and role/floor permissions; add failure/clone alerts; support maintenance, emergency/medical incident handling, rescue dispatch, and safe door-command guards.
5. **Implement interface recovery and reporting** — validate incoming snapshots, stop commands while disconnected, reconcile after reconnect, and expose fleet/request/audit/metrics views.
6. **Verify and document** — add focused tests for the specified baseline and document setup, limitations, and how to run the simulator/tests.

## Initial file structure

```text
srems/
    __init__.py
    __main__.py
    config.py
    models.py
    storage.py
    audit.py
    access.py
    dispatch.py
    elevator_interface.py
    service.py
tests/
    test_dispatch.py
    test_access.py
    test_operations.py
    test_audit.py
docs/
    implementation-plan.md
```

## Acceptance checks

- Hall calls work without a card; B1 is the no-card exception; E5 never serves B1.
- Dispatch considers car direction/commitments and uses a deterministic E1-to-E4 tie-break.
- Restricted destinations are authorized before any elevator command; blocked cars receive no ordinary assignment.
- Medical/emergency, maintenance, and communication loss preserve and reconcile affected requests rather than silently completing them.
- Door-close is inhibited during obstruction, and door-open requires a valid floor state.
- Audit integrity checking reports tampering; persistent history and wait-time metrics are available.
- The package imports and runs with the Python standard library, and focused tests pass.
