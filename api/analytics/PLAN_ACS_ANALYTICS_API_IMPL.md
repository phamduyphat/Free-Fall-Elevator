# Implementation Plan: HTTP APIs for `acs`, `analytics`, Virtual Clock, and Database Pool Lifecycle

This document defines the finalized implementation plan for exposing the `acs` and `analytics` modules over FastAPI inside the `api/` package, including the server-side tick-based virtual clock ($T_{\text{sim}}$), incorporating all review decisions.

---

## 1. Architectural Principles

1. **Grouped `api/acs/` and `api/analytics/` layout with `/api/v1/acs/` and `/api/v1/analytics/` prefixes:**
   - Keep `api/__init__.py` unchanged and limit `api/routes.py` edits to mounting `system_router`, `acs_router` (under `/acs`), and `analytics_router` (under `/analytics`), while preserving the existing unauthenticated `GET /api/v1/fleet/status` and `GET /api/v1/health` routes.
   - Organize all domain endpoints cleanly inside `api/acs/` and `api/analytics/`.
   - **Scope note:** This iteration targets the `api` module only (`main.py` already calls `DbConnectionPool.init_pool()` / `close_pool()` in `lifespan` and mounts `api.routes.router` at `/api/v1`, so no `main.py` edits are required).
2. **Readable Data Model Contract (Direct Response Dataclasses + Explicit Request Models):**
   - Do **not** rewrite `acs/payloads.py` or `analytics/reports.py` dataclasses into Pydantic models, and do **not** generate Pydantic models programmatically at import time (`pydantic.create_model`).
   - Use FastAPI + Pydantic v2's native `@dataclass` support **directly** for all response types (`AuthorizationResult`, `IntegrityCheckResult`, `WaitTimeReport`, `TravelTimeReport`, `TripDistributionReport`, `CarUtilizationReport`, `DowntimeReport`, `SecuritySummaryReport`, `ExportPayload`).
   - Declare a small, explicit, readable set of Pydantic `BaseModel` HTTP request classes (listed in Section 8) so clients supply only business inputs — never server-resolved `credential_id` / `admin_credential_id`, endpoint-fixed `is_dashboard_access`, or client-forged `timestamp` values.
3. **Server-Side Tick-Based Virtual Clock (`simulation/clock.py`):**
   - **No endpoint receives `timestamp` from clients** (except historical range filters `start_time` / `end_time` on log/analytics queries and optional `report_time` on `/analytics/downtime`). All event, auth, audit, and export timestamps are injected server-side from `SimulationClock.current_time` (via `SimulationClock.now()`).
   - `SimulationClock` lives in a dedicated top-level module (`<project root>/simulation/clock.py`) with **baked-in default constants**:
     - `DEFAULT_BASE_TIME = datetime(2026, 1, 1, 0, 0, 0)` (Default Test Run)
     - `MORNING_PEAK_BASE_TIME = datetime(2026, 1, 1, 7, 0, 0)` (`PERF-8` 07:00–09:00)
     - `EVENING_PEAK_BASE_TIME = datetime(2026, 1, 1, 17, 0, 0)` (`PERF-8` 17:00–20:00)
     - `OFF_PEAK_BASE_TIME = datetime(2026, 1, 1, 22, 0, 0)` (`PERF-8` 22:00–05:00)
     - `DEFAULT_SIM_SECONDS_PER_TICK = 1.0` ($\Delta t_{\text{sim\_seconds}}$)
     - `DEFAULT_TICK_INTERVAL_SECONDS = 1.0` (actual wall-clock seconds between ticks)
   - Virtual time formula:
     $$T_{\text{sim}} = T_0 + (\text{tick} \times \Delta t_{\text{sim\_seconds}})$$
     - **$T_0$ (`base_time`):** Normalized naive UTC datetime anchor (defaults to `DEFAULT_BASE_TIME`).
     - **$\text{tick}$:** Monotonic integer tick counter maintained in-memory by the simulation loop (starts at `0` on init/reset).
     - **$\Delta t_{\text{sim\_seconds}}$ (`sim_seconds_per_tick`):** Virtual seconds advanced per tick (defaults to `DEFAULT_SIM_SECONDS_PER_TICK`).
     - **`tick_interval_seconds`:** Actual wall-clock delay between ticks for demonstration speed control (defaults to `DEFAULT_TICK_INTERVAL_SECONDS`; `None` or `<= 0` pauses background auto-ticking for manual stepping).
   - **Background Thread & Sequential Tick-Then-Sleep Loop:**
     - Runs in a daemon `threading.Thread` so the tick loop never halts FastAPI's `asyncio` event loop (`PERF-1`–`PERF-3`).
     - Exposes a lock-guarded public class attribute (`SimulationClock.current_time: datetime`, plus `SimulationClock.now()` / `SimulationClock.snapshot()`) updated atomically under a `threading.Lock` on every tick and reset.
     - Each loop iteration executes **sequentially** before sleeping:
       1. Run the tick action hook (empty no-op stub for now; when future `asyncpg` / `acs` actions are added, dispatch via `asyncio.run_coroutine_threadsafe(..., main_loop).result()` so DB actions finish before the next tick while keeping the thread isolated from FastAPI's loop).
       2. Increment `tick += 1` and update `current_time = base_time + timedelta(seconds=tick * sim_seconds_per_tick)` under lock.
       3. Wait `stop_event.wait(timeout=tick_interval_seconds)` **only after** step 1 & 2 finish, preventing tick pile-up / queue blow-up (`PERF-5`) while allowing `reset()` / shutdown to wake the thread immediately.
4. **Card-based authentication has dedicated endpoints:**
   In-cabin card presentation (`CabAccessControl.validate_card_swipe`), dashboard card login (`DashboardAccessControl.validate_card_swipe`), and dashboard logout (`DashboardAccessControl.logout_dashboard_session`) live in `api/acs/auth.py`.
5. **No action-authorization middleware:**
   Do **not** use FastAPI/Starlette middleware for action authorization. Every endpoint explicitly calls the appropriate `acs` authorization check function inside the handler.
6. **Opaque Bearer `session_token` & Server-Side Session Store Lookup:**
   - The `Authorization: Bearer <session_token>` header carries only the opaque server-issued `session_token` (the token itself does **not** embed or store the card ID).
   - Every dashboard-protected route calls `DashboardAccessControl.validate_dashboard_operation(DashboardOperationPayload(session_token=token, op_type=..., timestamp=SimulationClock.now()), db_pool=db_pool)` to verify TTL and role permissions against `DashboardAccessControl.active_dashboard_sessions`, and reads the bound `credential_id`, `user_id`, and `role` from that existing server-side in-memory session store (`DashboardAccessControl.active_dashboard_sessions` / the returned `AuthorizationResult`).
7. **Semantic HTTP error codes returning the full `AuthorizationResult` payload:**
   When `AuthorizationResult.allowed` is `False`, return a `JSONResponse` with a semantic HTTP status code **and the entire `AuthorizationResult` serialized as the top-level JSON body** (not nested under `{"detail": ...}`):
   - `400 Bad Request`: `MALFORMED_MSG_REJECTED`, `CONFIRMATION_REQUIRED`
   - `401 Unauthorized`: `SESSION_INVALID`, `SESSION_EXPIRED`, `UNREGISTERED_CARD`, `NO_ACTIVE_CREDENTIAL`, `TOKEN_WINDOW_EXPIRED`
   - `403 Forbidden`: `UNAUTHORIZED_ROLE`, `UNAUTHORIZED_CMD_REJECTED`, `DASHBOARD_ACCESS_DENIED`, `PEER_ADMIN_REQUIRED`, `UNAUTHORIZED_FLOOR`, `E5_B1_FORBIDDEN`, `CARD_SUSPENDED`, `CARD_REVOKED`, `CARD_EXPIRED`, `ONE_TAP_LIMIT_EXCEEDED`
   - `409 Conflict`: `INVALID_STATE_TRANSITION`, `CREDENTIAL_ALREADY_EXISTS`, `REPLACEMENT_ID_IN_USE`, `COMMAND_CONFLICT_REJECTED`
   - `503 Service Unavailable`: `DB_FAIL_CLOSED`
8. **No extra API-layer audit logging for `VIEW_*` operations:**
   Underlying `acs` and `analytics` public methods already emit the required audit records where applicable; the API layer does not add `VIEW_*` entries to `AuditActionType` or duplicate logging logic.
9. **Database pool dependency injection & readiness:**
   Every route handler injects `db_pool: type[DbConnectionPool] = Depends(get_db_pool)` from `db.py` and forwards `db_pool=db_pool`. When `api` is mounted or tested standalone without `main.py`, callers invoke `await DbConnectionPool.init_pool()` / `close_pool()` in their own app lifespan or test fixture.

---

## 2. Proposed Directory Structure (`simulation/` & `api/`)

```text
simulation/
├── __init__.py               # Package export for SimulationClock
└── clock.py                  # Standalone virtual clock module with baked-in defaults, public current_time, and daemon thread loop

api/
├── __init__.py               # Unchanged
├── routes.py                 # Minimal edits: keep /health & /fleet/status, mount system, acs (/acs), and analytics (/analytics)
├── helpers.py                # Bearer extraction, authorize_dashboard_op(), semantic AuthorizationResult -> JSONResponse mapper
├── system.py                 # DB pool readiness (/health/db), clock inspection (/system/clock), ADMIN reset (/system/reset)
├── acs/
│   ├── __init__.py           # Combines acs sub-routers into acs_router (prefix="/acs")
│   ├── auth.py               # Card-based auth: cab swipe, dashboard login, dashboard logout
│   ├── cab.py                # In-cabin destination selection & E5 service run
│   ├── credentials.py        # Single /credentials/manage endpoint & /credentials/replace-lost
│   ├── fleet.py              # Maintenance, return-to-service, emergency override, rescue dispatch, view auth endpoints
│   └── logs.py               # Time-range searchable authorization events, alerts, audit logs, incidents, audit verification
└── analytics/
    ├── __init__.py           # Combines analytics sub-routers into analytics_router (prefix="/analytics")
    ├── metrics.py            # Traffic, Fleet, and Security KPI endpoints
    └── export.py             # CSV/JSON report export returning JSON-serialized ExportPayload
```

---

## 3. Prerequisite Addition to `acs/` (`acs/enums.py`)

Add a single permission member to `DashboardOperationType` in `acs/enums.py` (no changes to `AuditActionType`):
- Add `VIEW_ANALYTICS = "VIEW_ANALYTICS"` to `DashboardOperationType`.
- Grant `DashboardOperationType.VIEW_ANALYTICS: True` in `DASHBOARD_PERMISSIONS` for `UserRole.ADMIN` only (`False` for `TECHNICIAN` and all other roles — matching `EXPORT_ANALYTICS_REPORTS`).

---

## 4. Virtual Clock, Database Pool & System Endpoints (`db.py`, `simulation/clock.py`, `api/system.py`)

| Method & Path | Auth / Gate | Underlying Call / Behavior | Notes |
| :--- | :--- | :--- | :--- |
| `GET /api/v1/health` | Unauthenticated (kept in `api/routes.py`) | Returns `HealthResponse`. | Existing liveness endpoint. |
| `GET /api/v1/health/db` | Unauthenticated | Acquires connection from `db_pool` (`Depends(get_db_pool)`) and executes `SELECT 1`. | Returns `503` if pool is uninitialized or unreachable. |
| `GET /api/v1/system/clock` | Unauthenticated | Reads `SimulationClock.snapshot()`. | Returns `current_sim_time` (`SimulationClock.current_time`), `base_time` ($T_0$), `tick`, `sim_seconds_per_tick`, `tick_interval_seconds`, and `running`. |
| `POST /api/v1/system/reset` | Bearer `session_token` -> `ADMIN` (`DashboardAccessControl.validate_dashboard_operation(..., EMERGENCY_OVERRIDE, timestamp=SimulationClock.now())`) | 1. Signals & joins active clock thread<br>2. Calls `await db_pool.reset_database_tables()`, `CabAccessControl.reset_in_memory_state()`, `DashboardAccessControl.reset_in_memory_state()`<br>3. Resets & starts `SimulationClock` thread with `base_time`, `sim_seconds_per_tick`, `tick_interval_seconds` | Body: `SystemResetRequest` (defaults to clock module constants: `DEFAULT_BASE_TIME`, `DEFAULT_SIM_SECONDS_PER_TICK`, `DEFAULT_TICK_INTERVAL_SECONDS`). Resets `tick = 0`, updates `current_time = base_time`, and starts daemon thread if `tick_interval_seconds > 0`. |
| `POST /api/v1/system/clock/tick` *(optional helper)* | Bearer `session_token` -> `ADMIN` | Executes `steps: int = 1` sequential tick iterations synchronously. | Useful for deterministic automated tests or stepped demos when `tick_interval_seconds` is `0`/`None`. |

---

## 5. Card-Based Authentication Endpoints (`api/acs/auth.py`)

All timestamps are injected server-side via `SimulationClock.now()`:

| Method & Path | HTTP Body | Underlying `acs` Call | Notes |
| :--- | :--- | :--- | :--- |
| `POST /api/v1/acs/auth/cab/swipe` | `CabSwipeRequest` | `CabAccessControl.validate_card_swipe(CardSwipePayload(..., is_dashboard_access=False, timestamp=SimulationClock.now()), db_pool=db_pool)` | Issues per-car `session_token` (`5s` or `30s` window) stored in `CabAccessControl.active_car_sessions` and returns full `AuthorizationResult`. |
| `POST /api/v1/acs/auth/dashboard/login` | `DashboardLoginRequest` | `DashboardAccessControl.validate_card_swipe(CardSwipePayload(..., is_dashboard_access=True, timestamp=SimulationClock.now()), db_pool=db_pool)` | Issues 15-minute sliding-TTL `session_token` stored in `DashboardAccessControl.active_dashboard_sessions` for `ADMIN` or `TECHNICIAN` and returns full `AuthorizationResult`. |
| `POST /api/v1/acs/auth/dashboard/logout` | Empty body + `Authorization: Bearer <token>` | `DashboardAccessControl.logout_dashboard_session(token, timestamp=SimulationClock.now(), db_pool=db_pool)` | Terminates session in `DashboardAccessControl.active_dashboard_sessions`, emits `ADMIN_LOGOUT (MANUAL)`, and returns full `AuthorizationResult`. |

---

## 6. `acs` Domain Endpoints (`/api/v1/acs/...`)

All action timestamps and dashboard operation checks use `SimulationClock.now()`.

### 6.1 In-Cabin Elevator Operations (`api/acs/cab.py`)
| Method & Path | HTTP Body | Underlying `acs` Call |
| :--- | :--- | :--- |
| `POST /api/v1/acs/cab/destination` | `CabDestinationRequest` | `CabAccessControl.validate_car_destination(CarDestinationRequestPayload(..., timestamp=SimulationClock.now()), db_pool=db_pool)` |
| `POST /api/v1/acs/cab/e5/service-run` | `E5ServiceRunRequest` | `CabAccessControl.validate_e5_service_run(E5ServiceRunPayload(..., timestamp=SimulationClock.now()), db_pool=db_pool)` |

### 6.2 Credential Lifecycle Management (`api/acs/credentials.py`)
All routes require `Authorization: Bearer <session_token>` -> `DashboardAccessControl.validate_dashboard_operation(..., MANAGE_CREDENTIALS, timestamp=SimulationClock.now())`, then read `credential_id` from `DashboardAccessControl.active_dashboard_sessions`:

| Method & Path | HTTP Body | Underlying `acs` Calls |
| :--- | :--- | :--- |
| `POST /api/v1/acs/credentials/manage` | `CredentialManageRequest` | 1. `DashboardAccessControl.validate_dashboard_operation(..., MANAGE_CREDENTIALS, timestamp=now)`<br>2. `CredentialAccessControl.manage_credential(CredentialManagementPayload(admin_credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |
| `POST /api/v1/acs/credentials/replace-lost` | `ReplaceLostCardRequest` | 1. `DashboardAccessControl.validate_dashboard_operation(..., MANAGE_CREDENTIALS, timestamp=now)`<br>2. `CredentialAccessControl.replace_lost_card(RevokeAndReissuePayload(admin_credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |

### 6.3 Fleet Maintenance, Emergency & View Authorization (`api/acs/fleet.py`)
- Existing `GET /api/v1/fleet/status` in `api/routes.py` remains **unauthenticated** for now.
- All routes below require `Authorization: Bearer <session_token>` and resolve `credential_id` from `DashboardAccessControl.active_dashboard_sessions` via `validate_dashboard_operation(..., timestamp=SimulationClock.now())`:

| Method & Path | HTTP Body | `DashboardOperationType` Check | Underlying Action |
| :--- | :--- | :--- | :--- |
| `POST /api/v1/acs/fleet/maintenance` | `MaintenanceOperationRequest` | `MAINTENANCE_CONTROL` (`ADMIN`, `TECHNICIAN`) | `FleetAccessControl.validate_maintenance_operation(MaintenanceOperationPayload(credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |
| `POST /api/v1/acs/fleet/return-to-service` | `ReturnToServiceRequest` | `MAINTENANCE_CONTROL` (`ADMIN`, `TECHNICIAN`) | `FleetAccessControl.validate_return_to_service(ReturnToServicePayload(credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |
| `POST /api/v1/acs/fleet/emergency-override` | `EmergencyOverrideRequest` | `EMERGENCY_OVERRIDE` (`ADMIN` only) | `FleetAccessControl.validate_emergency_override(EmergencyOverridePayload(credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |
| `POST /api/v1/acs/fleet/rescue-dispatch` | `RescueDispatchRequest` | `EMERGENCY_OVERRIDE` (`ADMIN` only) | `FleetAccessControl.validate_rescue_car_dispatch(RescueCarDispatchPayload(credential_id=session_cred_id, ..., timestamp=now), db_pool=db_pool)` |
| `GET /api/v1/acs/fleet/view` | — | `VIEW_FLEET_STATUS` (`ADMIN`, `TECHNICIAN`) | Returns `AuthorizationResult` after `validate_dashboard_operation` check. |
| `GET /api/v1/acs/fleet/diagnostics` | — | `VIEW_DIAGNOSTICS` (`ADMIN`, `TECHNICIAN`) | Returns `AuthorizationResult` after `validate_dashboard_operation` check. |

### 6.4 Security Logs, Alerts, Incidents & Audit Verification (`api/acs/logs.py`)
All routes require `Authorization: Bearer <session_token>` validated via `DashboardAccessControl.validate_dashboard_operation(..., timestamp=SimulationClock.now())`:

| Method & Path | Query Params / Body | `DashboardOperationType` Check | Underlying `Logging` / `DashboardAccessControl` Call |
| :--- | :--- | :--- | :--- |
| `GET /api/v1/acs/logs/authorization-events` | `credential_id?`, `elevator_id?`, `outcome?`, `denial_reason?`, `start_time?`, `end_time?`, `limit=100` | `VIEW_AUDIT_LOGS` (`ADMIN` only) | `Logging.search_authorization_events(..., db_pool=db_pool)` |
| `GET /api/v1/acs/logs/audit` | `actor_id?`, `action_type?`, `target_id?`, `start_time?`, `end_time?`, `limit=100` | `VIEW_AUDIT_LOGS` (`ADMIN` only) | `Logging.search_audit_logs(..., db_pool=db_pool)` |
| `GET /api/v1/acs/logs/incidents` | `status?`, `emergency_type?`, `elevator_id?`, `start_time?`, `end_time?`, `limit=100` | `VIEW_AUDIT_LOGS` (`ADMIN` only) | `Logging.search_incidents(..., db_pool=db_pool)` |
| `GET /api/v1/acs/logs/alerts` | `status?`, `severity?`, `alert_type?`, `credential_id?`, `start_time?`, `end_time?`, `limit=100` | `VIEW_DIAGNOSTICS` (`ADMIN`, `TECHNICIAN`) | `Logging.search_security_alerts(..., db_pool=db_pool)` |
| `PATCH /api/v1/acs/logs/alerts/{alert_id}` | `AlertStatusUpdateRequest` | `EMERGENCY_OVERRIDE` (`ADMIN` only) | `Logging.update_security_alert_status(alert_id=..., new_status=..., admin_user_id=session_user_id, timestamp=SimulationClock.now(), db_pool=db_pool)` |
| `POST /api/v1/acs/logs/audit/verify` | Empty body | `RUN_AUDIT_INTEGRITY` (`ADMIN` only) | `DashboardAccessControl.run_audit_integrity_check(admin_credential_id=session_cred_id, timestamp=SimulationClock.now(), db_pool=db_pool)` |

> **Note (`SEC-2.14` / `ANA-5.7`):** `limit` should permit higher bounds (e.g., `1 <= limit <= 10000`, default `100`) so multi-hour tick simulations can query full time-range windows without silent truncation.

---

## 7. `analytics` Domain Endpoints (`/api/v1/analytics/...`)

All routes require `Authorization: Bearer <session_token>` validated via `DashboardAccessControl.validate_dashboard_operation(..., timestamp=SimulationClock.now())`:

### 7.1 KPI Endpoints (`api/analytics/metrics.py`)
Gated by `DashboardOperationType.VIEW_ANALYTICS` (`ADMIN` only). No client `timestamp` query parameter; time ranges use `[start_time, end_time]`:

| Method & Path | Query Params | Underlying `Metrics` Call | Response Model |
| :--- | :--- | :--- | :--- |
| `GET /api/v1/analytics/wait-times` | `start_time`, `end_time`, `traffic_profile?` | `Metrics.get_wait_time_kpis(..., db_pool=db_pool)` | `WaitTimeReport` |
| `GET /api/v1/analytics/travel-times` | `start_time`, `end_time`, `elevator_id?` | `Metrics.get_travel_time_kpis(..., db_pool=db_pool)` | `TravelTimeReport` |
| `GET /api/v1/analytics/trip-distribution` | `start_time`, `end_time`, `elevator_id?`, `traffic_profile?` | `Metrics.get_trip_distribution(..., db_pool=db_pool)` | `TripDistributionReport` |
| `GET /api/v1/analytics/utilization` | `start_time`, `end_time`, `floor_height_meters=3.0` | `Metrics.get_utilization_kpis(..., db_pool=db_pool)` | `dict[str, CarUtilizationReport]` |
| `GET /api/v1/analytics/downtime` | `start_time`, `end_time`, `report_time?` | `Metrics.get_downtime_summary(..., report_time=report_time or SimulationClock.now(), db_pool=db_pool)` | `DowntimeReport` |
| `GET /api/v1/analytics/security` | `start_time`, `end_time` | `Metrics.get_security_kpis(..., db_pool=db_pool)` | `SecuritySummaryReport` |

### 7.2 Report Export Endpoint (`api/analytics/export.py`)
Gated by `DashboardOperationType.EXPORT_ANALYTICS_REPORTS` (`ADMIN` only):

| Method & Path | Request Body | Underlying `ExportEngine` Call | Response |
| :--- | :--- | :--- | :--- |
| `POST /api/v1/analytics/export` | `AnalyticsExportRequest` | `ExportEngine.export_run_report(..., admin_id=session_cred_id, admin_role=session_role, current_time=SimulationClock.now(), db_pool=db_pool)` | JSON-serialized `ExportPayload` (with raw CSV or JSON string in `content`). |

---

## 8. Explicit HTTP Request `BaseModel` Inventory

To preserve readability without `create_model` metaprogramming or modifying `acs`/`analytics` dataclasses, and to keep all event timestamps controlled by `SimulationClock.now()`, only these lightweight Pydantic `BaseModel` classes are defined (colocated with their routers):

| Route Module | Pydantic `BaseModel` | Fields | Maps To |
| :--- | :--- | :--- | :--- |
| `api/system.py` | `SystemResetRequest` | `base_time: datetime = DEFAULT_BASE_TIME`, `sim_seconds_per_tick: float = DEFAULT_SIM_SECONDS_PER_TICK`, `tick_interval_seconds: Optional[float] = DEFAULT_TICK_INTERVAL_SECONDS` | `SimulationClock.reset(...)` + `DbConnectionPool.reset_database_tables()` |
| `api/system.py` | `ManualTickRequest` | `steps: int = 1` *(optional endpoint if manual stepping is enabled)* | `SimulationClock.advance(steps=...)` |
| `api/acs/auth.py` | `CabSwipeRequest` | `elevator_id: Optional[ElevatorId \| str] = None`, `credential_id: str`, `origin_floor: int = 1` | `CardSwipePayload(..., is_dashboard_access=False, timestamp=now)` |
| `api/acs/auth.py` | `DashboardLoginRequest` | `credential_id: str` | `CardSwipePayload(..., is_dashboard_access=True, timestamp=now)` |
| `api/acs/cab.py` | `CabDestinationRequest` | `elevator_id: ElevatorId \| str`, `origin_floor: int`, `requested_floor: int` | `CarDestinationRequestPayload(..., timestamp=now)` |
| `api/acs/cab.py` | `E5ServiceRunRequest` | `elevator_id: ElevatorId \| str`, `destination_floor: int`, `purpose: str` | `E5ServiceRunPayload(..., timestamp=now)` |
| `api/acs/credentials.py` | `CredentialManageRequest` | `action: CredentialAction \| str`, `target_user_id: Optional[str] = None`, `target_credential_id: Optional[str] = None`, `payload_details: dict[str, Any] = {}` | `CredentialManagementPayload(admin_credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/credentials.py` | `ReplaceLostCardRequest` | `lost_credential_id: str`, `replacement_credential_id: str`, `reason: str = "LOST_AND_REPLACED"` | `RevokeAndReissuePayload(admin_credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/fleet.py` | `MaintenanceOperationRequest` | `elevator_id: ElevatorId \| str`, `operation_type: MaintenanceOperationType \| str` | `MaintenanceOperationPayload(credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/fleet.py` | `ReturnToServiceRequest` | `elevator_id: ElevatorId \| str`, `technician_confirmation: bool` | `ReturnToServicePayload(credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/fleet.py` | `EmergencyOverrideRequest` | `override_type: EmergencyOverrideType \| str`, `target_car_id: Optional[ElevatorId \| str] = None`, `incident_id: Optional[str] = None`, `reason: str` | `EmergencyOverridePayload(credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/fleet.py` | `RescueDispatchRequest` | `incident_id: str`, `rescue_car_id: ElevatorId \| str`, `incident_floor: int`, `dwell_wait_seconds: int = 120` | `RescueCarDispatchPayload(credential_id=session_cred_id, ..., timestamp=now)` |
| `api/acs/logs.py` | `AlertStatusUpdateRequest` | `new_status: AlertStatus \| str` | `Logging.update_security_alert_status(..., timestamp=now)` |
| `api/analytics/export.py` | `AnalyticsExportRequest` | `run_id: str`, `export_format: Literal["JSON", "CSV"] = "JSON"`, `include_per_request: bool = False`, `start_time: Optional[datetime] = None`, `end_time: Optional[datetime] = None` | `ExportEngine.export_run_report(..., current_time=now)` |

**All** HTTP response models (`AuthorizationResult`, `IntegrityCheckResult`, all `analytics.reports.*` dataclasses, `ExportPayload`) reuse existing dataclasses directly.

---

## 9. Implementation Steps & Retained Test Suite

1. **Extend `acs/enums.py`:** Add `DashboardOperationType.VIEW_ANALYTICS` and grant it to `UserRole.ADMIN` only in `DASHBOARD_PERMISSIONS`.
2. **Create `simulation/clock.py` (and `simulation/__init__.py`):** Implement `SimulationClock` with baked-in default constants (`DEFAULT_BASE_TIME`, `MORNING_PEAK_BASE_TIME`, `EVENING_PEAK_BASE_TIME`, `OFF_PEAK_BASE_TIME`, `DEFAULT_SIM_SECONDS_PER_TICK`, `DEFAULT_TICK_INTERVAL_SECONDS`), lock-protected public `current_time`, and a daemon `threading.Thread` sequential tick-then-sleep loop (`action_hook()` -> `tick += 1` / update `current_time` -> `stop_event.wait(tick_interval_seconds)`), keeping the tick action hook empty for now.
3. **Create `api/helpers.py`:** Implement Bearer token extraction, `authorize_dashboard_op(...)` (validating via `DashboardAccessControl.validate_dashboard_operation(..., timestamp=SimulationClock.now())` and resolving server-side session fields from `DashboardAccessControl.active_dashboard_sessions`), and `auth_result_response(...)` (mapping `AuthorizationResult` to semantic HTTP status codes while preserving the full `AuthorizationResult` JSON body).
4. **Create `api/system.py`:** Implement `/health/db`, `GET /system/clock`, `ADMIN`-gated `POST /system/reset` (`base_time`, `sim_seconds_per_tick`, `tick_interval_seconds`), and optional `POST /system/clock/tick`.
5. **Create `api/acs/` package:** Implement `auth.py`, `cab.py`, `credentials.py`, `fleet.py`, `logs.py`, and `api/acs/__init__.py`.
6. **Create `api/analytics/` package:** Implement `metrics.py`, `export.py`, and `api/analytics/__init__.py`.
7. **Wire routers in `api/routes.py`:** Minimal update to include `system_router`, `acs_router` (`/acs`), and `analytics_router` (`/analytics`) alongside existing `/health` and `/fleet/status`.
8. **Create & retain tests in `tests/`:** Add persistent `pytest` / `httpx` test files (retained in `tests/` for manual test runs) covering `simulation/clock.py` sequential thread ticking, lock-safe `current_time` reads, & `PERF-8` base-time resets, DB pool readiness/reset (`ADMIN`-gated), card swipe auth, server-side session lookup via opaque Bearer tokens, per-endpoint authorization denials with semantic HTTP status + full `AuthorizationResult` payloads, credential lifecycle, fleet/maintenance ops, time-range log/alert queries, audit verification, analytics KPIs, and JSON/CSV export payloads.
