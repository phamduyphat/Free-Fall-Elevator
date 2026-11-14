# Mode 1 integration: software test-stub baseline

## Scope and evidence

This patch adds a **software-only**, process-local test stub. Its events and car positions are **not physical elevator telemetry** and should not be used to make safety claims. The API labels the source `labelled-test-stub` to comply with SRS SWI-2.

| SRS requirement | Implemented in this patch | Scope / limitation |
|---|---|---|
| FR-1.1 | Registered E1–E5 | E1–E4 residential, E5 freight/service |
| FR-1.2, FR-1.11 | Current position, direction, door, availability, next stop | Software test stub only; not physical device |
| FR-1.3 | Ordinary UP/DOWN and E5 FREIGHT hall calls without a card | Basic validation, B1/upper-floor direction rules |
| FR-1.4, FR-1.5 | Dispatch uses predicted pickup distance, current direction, ordered commitments, availability and lower-ID ties | Simplified one-floor-per-second prediction; not a traffic-optimized algorithm |
| FR-1.6, FR-1.23 | Excludes unavailable E1–E4; reassigns pending calls | Isolation events from trusted server logic and Admin test-stub endpoint |
| FR-1.7 | Received, queued, assigned, approaching, arrived, completed, and requeue history | Cancellation and emergency reconciliation remain future work |
| FR-1.8, FR-1.9 | Authorized car destinations are sent to the test stub | Permission comes from existing ACS, and car calls are not yet persisted |
| FR-1.10, FR-1.13 | B1 without card for E1–E4, never E5; E5 freight calls and exclusive service runs | Cabin service runs are authorized by existing ACS |
| FR-1.12, FR-1.18 | Arrival timestamps and wait seconds from accepted hall call | In-memory timestamp data; not yet persisted to Analytics |
| FR-1.22 | Active identical hall calls are merged | `NO_BOARDING` and repeated-abuse detection not yet wired |
| SWI-2 | Every operational response includes an explicit test-stub source | Hardware/equipment adapter remains unimplemented |

**Not implemented by this patch:** a role-restricted production dashboard (the demo fleet endpoint is public), persisted records of rejected freight calls, actual elevator interface/protocol mapping (COM-1 to COM-4), physical safety interlocks, equipment sensor validation, emergency handling and incident lifecycle (Mode 3), full fault recovery/maintenance records (Mode 4), NO_BOARDING detection, persistent operations events/analytics linkage (Mode 5), and complete end-to-end SRS validation. Advanced freight/priority and real fleet traffic simulation are also outside this test-stub baseline.

## API examples

Public passenger hall calls require no card:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/operational/hall-calls \
  -H 'Content-Type: application/json' \
  -d '{"origin_floor":7,"direction":"UP","call_type":"ORDINARY"}'

curl http://127.0.0.1:8000/api/v1/fleet/status
curl http://127.0.0.1:8000/api/v1/operational/hall-calls
```

B1 is represented by `-1` in the API and `B1` in the frontend, and E5 does not serve B1. Freight requests omit `direction` and require `call_type: "FREIGHT"`.

A restricted car destination requires a **successful ACS card presentation**:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/acs/auth/cab/swipe \
  -H 'Content-Type: application/json' \
  -d '{"elevator_id":"E1","credential_id":"ADMIN-CARD-0001","origin_floor":1}'

curl -X POST http://127.0.0.1:8000/api/v1/acs/cab/destination \
  -H 'Content-Type: application/json' \
  -d '{"elevator_id":"E1","origin_floor":1,"requested_floor":12}'
```

The ACS and `ADMIN-CARD-0001` depend on PostgreSQL having been set up by `seed.sql`. If the ACS denies a destination, it does not create a new operational car call.

Admin-only test-stub control uses the existing dashboard login bearer token:

```text
POST /api/v1/operational/stub/advance  {"seconds": 10}
POST /api/v1/operational/stub/state    {"elevator_id":"E1", "state":"MAINTENANCE"}
```

These endpoints are for synthetic demonstrations only. Do not expose them in an untrusted production deployment. By default, the server advances its *test-stub* clock lazily according to elapsed real time when the API is polled. No physical equipment is commanded.

## Run and test

Run the project using its existing `README.md` and `seed.sql` instructions. The frontend starts in `frontend/` with `npm install && npm run dev` and proxies `/api` to the backend. The **Live simulation** screen polls fleet state and call records instead of generating local positions.

The database-free Mode 1 unit tests are independently executable:

```bash
python -m unittest discover -s tests -p test_operational.py -v
```

Other project tests require the existing PostgreSQL database and `asyncpg` package. Resetting through `/api/v1/system/reset` clears the test-stub state; restarting the API also clears all process-local operational state. The virtual `SimulationClock` endpoints and the Mode 1 test-stub's real-time stepping mechanism are distinct in this patch.
