# SREMS HTTP API Documentation (Modes 2 & 5)

Base router mounted at `/api/v1` (`main.py` -> `api/routes.py`). Interactive OpenAPI/Swagger docs are served at `/docs`.

---

## 1. Common Conventions

### 1.1 Authentication & Sessions
- **Opaque Bearer Token (`DashboardAccessControl`):** Dashboard endpoints require `Authorization: Bearer <session_token>`.
- **Session Lifecycle:** Tokens are UUID strings issued by `POST /api/v1/acs/auth/dashboard/login` (`ADMIN` or `TECHNICIAN` only) and resolved server-side from `DashboardAccessControl.active_dashboard_sessions`. Tokens carry a **15-minute (900s) sliding TTL** evaluated against `SimulationClock.now()`.
- **Cab Authentication (`CabAccessControl`):** In-cabin endpoints (`/api/v1/acs/auth/cab/swipe`, `/api/v1/acs/cab/destination`, `/api/v1/acs/cab/e5/service-run`) do **not** use Bearer headers; they rely on single-use per-car sessions (`CabAccessControl.active_car_sessions`) established by swiping a card in the cab (5s window for `RESIDENT`/`VISITOR`; 30s window for `BUILDING_SERVICE`/`TECHNICIAN`).

### 1.2 Virtual Simulation Clock
- Action and write endpoints inject timestamps server-side from `SimulationClock.now()` (or `session.timestamp` captured at request entry); clients do **not** submit timestamps on action payloads.
- Query and analytics endpoints accept ISO-8601 `start_time` / `end_time` filters (normalized to naive UTC).

### 1.3 Standard `AuthorizationResult` Schema
All ACS decision endpoints return `AuthorizationResult`:

```json
{
  "outcome": "ALLOWED",
  "allowed": true,
  "reason_code": null,
  "message": "Access granted.",
  "session_token": null,
  "credential_id": "ADMIN-CARD-0001",
  "alert_triggered": false,
  "details": {}
}
```

### 1.4 HTTP Status Code Mapping (`api/helpers.py`)
Denied requests return semantic HTTP status codes with the full `AuthorizationResult` JSON object as the response body:

| HTTP Status | `DenialReason` Values |
| :--- | :--- |
| `200 OK` | Request allowed (`allowed: true`) |
| `400 Bad Request` | `MALFORMED_MSG_REJECTED`, `CONFIRMATION_REQUIRED` |
| `401 Unauthorized` | `SESSION_INVALID`, `SESSION_EXPIRED`, `UNREGISTERED_CARD`, `NO_ACTIVE_CREDENTIAL`, `TOKEN_WINDOW_EXPIRED` |
| `403 Forbidden` | `UNAUTHORIZED_ROLE`, `UNAUTHORIZED_CMD_REJECTED`, `DASHBOARD_ACCESS_DENIED`, `PEER_ADMIN_REQUIRED`, `UNAUTHORIZED_FLOOR`, `E5_B1_FORBIDDEN`, `CARD_SUSPENDED`, `CARD_REVOKED`, `CARD_EXPIRED`, `ONE_TAP_LIMIT_EXCEEDED` (and fallback) |
| `404 Not Found` | Unknown `alert_id` on `PATCH /api/v1/acs/logs/alerts/{alert_id}` |
| `409 Conflict` | `INVALID_STATE_TRANSITION`, `CREDENTIAL_ALREADY_EXISTS`, `REPLACEMENT_ID_IN_USE`, `COMMAND_CONFLICT_REJECTED` |
| `503 Service Unavailable` | `DB_FAIL_CLOSED` (or uninitialized DB pool on `/health/db`) |

### 1.5 Enum Reference
- **`ElevatorId`:** `E1`, `E2`, `E3`, `E4`, `E5` (Residential: `E1`–`E4`; Freight/Service: `E5`)
- **`UserRole`:** `RESIDENT`, `VISITOR`, `BUILDING_SERVICE`, `TECHNICIAN`, `ADMIN`, `SYSTEM`
- **`CredentialAction`:** `ISSUE`, `ISSUE_VISITOR_PASS`, `SUSPEND`, `REINSTATE`, `REVOKE`
- **`SuspensionReason`:** `LOST`, `CLONE_SUSPECTED`, `ADMIN_MANUAL`
- **`MaintenanceOperationType`:** `ENTER_MAINTENANCE`, `DIAGNOSTIC_QUERY`
- **`EmergencyOverrideType`:** `DOOR_OVERRIDE`, `MANUAL_RECALL`, `RESOLVE_INCIDENT`
- **`AlertSeverity`:** `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`
- **`AlertType`:** `REPEATED_FAILURES`, `CLONED_CREDENTIAL`, `CARD_SCANNING`, `LOST_CARD_USAGE`, `REVOKED_CARD_USAGE`, `DOOR_OBSTRUCTION`
- **`AlertStatus`:** `OPEN`, `ACKNOWLEDGED`, `RESOLVED`
- **`AuthorizationOutcome`:** `ALLOWED`, `DENIED`
- **`TrafficProfile`:** `PEAK`, `OFF_PEAK`, `NORMAL`, `STRESS`

---

## 2. Mode 2: Access Control & Security (`/api/v1/acs`)

### 2.1 Authentication (`api/acs/auth.py`)

#### `POST /api/v1/acs/auth/cab/swipe`
Validate an in-cabin card swipe at `SimulationClock.now()`, run clone detection (same card in two distinct cars within `< 10s`), and issue a single-use per-car presentation token.
- **Auth:** None
- **Request Body (`CabSwipeRequest`):**
  - `elevator_id` (`ElevatorId | str | null`, optional, default `null`) — must be a valid `E1`–`E5` ID.
  - `credential_id` (`string`, required)
  - `origin_floor` (`integer`, optional, default `1`)
- **Response (`200 AuthorizationResult`):**
  - `session_token`: per-car token UUID
  - `details`: `{ "elevator_id": "E1", "role": "RESIDENT", "allowed_floors": [-1, 1, 10], "expires_at": "..." }`
- **Common Denials:** `400 MALFORMED_MSG_REJECTED`, `401 UNREGISTERED_CARD`, `403 CARD_SUSPENDED`, `403 CARD_REVOKED`, `403 CARD_EXPIRED`

#### `POST /api/v1/acs/auth/dashboard/login`
Authenticate an `ADMIN` or `TECHNICIAN` card on the dashboard login reader and open a 15-minute sliding-TTL session.
- **Auth:** None
- **Request Body (`DashboardLoginRequest`):**
  - `credential_id` (`string`, required)
- **Response (`200 AuthorizationResult`):**
  - `session_token`: opaque Bearer token
  - `details`: `{ "user_id": "...", "role": "ADMIN", "expires_at": "..." }`
- **Common Denials:** `401 UNREGISTERED_CARD`, `403 DASHBOARD_ACCESS_DENIED` (e.g., `RESIDENT`), `403 CARD_SUSPENDED` / `CARD_REVOKED` / `CARD_EXPIRED`

#### `POST /api/v1/acs/auth/dashboard/logout`
Explicitly terminate the caller's active dashboard session and write an `ADMIN_LOGOUT` (`reason: "MANUAL"`) audit record.
- **Auth:** Bearer (`ADMIN` or `TECHNICIAN`)
- **Request Body:** None
- **Response (`200 AuthorizationResult`):** `details: { "reason": "MANUAL", "session_token": "..." }`
- **Denials:** `401 SESSION_INVALID`

---

### 2.2 Cab Operations (`api/acs/cab.py`)

#### `POST /api/v1/acs/cab/destination`
Validate an in-cabin destination button press at `SimulationClock.now()`.
- **Auth:** None (uses active per-car token; `requested_floor == -1` on `E1`–`E4` is accepted without a card)
- **Request Body (`CabDestinationRequest`):**
  - `elevator_id` (`ElevatorId | str`, required)
  - `origin_floor` (`integer`, required)
  - `requested_floor` (`integer`, required; valid range `-1` or `1`–`30`, `0` invalid)
- **Response (`200 AuthorizationResult`):**
  - `details` (authenticated): `{ "elevator_id", "origin_floor", "requested_floor", "role" }`
  - `details` (B1 exception): `{ "elevator_id", "origin_floor", "requested_floor": -1, "exception": "B1_NO_CARD" }`
- **Common Denials:** `400 MALFORMED_MSG_REJECTED`, `401 NO_ACTIVE_CREDENTIAL`, `401 TOKEN_WINDOW_EXPIRED`, `403 ONE_TAP_LIMIT_EXCEEDED`, `403 UNAUTHORIZED_FLOOR`, `403 E5_B1_FORBIDDEN`

#### `POST /api/v1/acs/cab/e5/service-run`
Validate a dedicated freight/service run on `E5` at `SimulationClock.now()`.
- **Auth:** None (requires active cab session on `E5` held by `BUILDING_SERVICE`, `TECHNICIAN`, or `ADMIN`)
- **Request Body (`E5ServiceRunRequest`):**
  - `elevator_id` (`ElevatorId | str`, required — must be `"E5"`)
  - `destination_floor` (`integer`, required — `B1`/`-1` forbidden; valid range `1`–`30`)
  - `purpose` (`string`, required)
- **Response (`200 AuthorizationResult`):**
  - `details`: `{ "elevator_id": "E5", "destination_floor", "purpose", "role" }`
- **Common Denials:** `400 MALFORMED_MSG_REJECTED`, `401 NO_ACTIVE_CREDENTIAL`, `401 TOKEN_WINDOW_EXPIRED`, `403 E5_B1_FORBIDDEN`, `403 ONE_TAP_LIMIT_EXCEEDED`, `403 UNAUTHORIZED_CMD_REJECTED` (e.g., `RESIDENT` or `VISITOR`)

---

### 2.3 Credential Lifecycle (`api/acs/credentials.py`)

#### `POST /api/v1/acs/credentials/manage`
Execute an Admin credential lifecycle action (`ISSUE`, `ISSUE_VISITOR_PASS`, `SUSPEND`, `REINSTATE`, or `REVOKE`). Peer-administration rules prevent an Admin from suspending or revoking their own card or another card tied to their `user_id`.
- **Auth:** Bearer (`ADMIN` only — `MANAGE_CREDENTIALS`)
- **Request Body (`CredentialManageRequest`):**
  - `action` (`CredentialAction | str`, required): `ISSUE` | `ISSUE_VISITOR_PASS` | `SUSPEND` | `REINSTATE` | `REVOKE`
  - `target_user_id` (`string | null`, optional; auto-creates a user row when omitted/unmatched on `ISSUE` / `ISSUE_VISITOR_PASS`)
  - `target_credential_id` (`string | null`, required by domain logic for all actions)
  - `payload_details` (`object`, optional, default `{}`):
    - For `ISSUE` / `ISSUE_VISITOR_PASS`: `full_name`, `role` (`ISSUE` defaults to existing user's role or `RESIDENT`; `ISSUE_VISITOR_PASS` forces `VISITOR`), `apartment_unit`, `assigned_floor`, `allowed_floors` (`list[int]`), `credential_type`, `expires_at` (ISO string/datetime), `duration_seconds` (visitor pass default `14400` / 4h).
    - For `SUSPEND`: `suspension_reason` (`LOST`, `CLONE_SUSPECTED`, or `ADMIN_MANUAL`; unrecognized/omitted defaults to `ADMIN_MANUAL`).
    - For `REINSTATE`: `notes` (`string`).
    - For `REVOKE`: `revocation_reason` (`string`, default `"ADMIN_ACTION"`).
- **Response (`200 AuthorizationResult`):**
  - On `ISSUE` / `ISSUE_VISITOR_PASS`: `details: { "user_id", "credential_id", "role", "allowed_floors", "expires_at" }`
  - On status transition: `credential_id` set to `target_credential_id`
- **Common Denials:** `400 MALFORMED_MSG_REJECTED`, `401 SESSION_INVALID` / `SESSION_EXPIRED` / `UNREGISTERED_CARD`, `403 UNAUTHORIZED_ROLE` / `PEER_ADMIN_REQUIRED`, `409 CREDENTIAL_ALREADY_EXISTS` / `INVALID_STATE_TRANSITION`

#### `POST /api/v1/acs/credentials/replace-lost`
Atomically revoke a lost credential and issue a replacement card inheriting `user_id`, `credential_type`, `expires_at`, and `allowed_floors`.
- **Auth:** Bearer (`ADMIN` only — `MANAGE_CREDENTIALS`; peer-admin enforced)
- **Request Body (`ReplaceLostCardRequest`):**
  - `lost_credential_id` (`string`, required)
  - `replacement_credential_id` (`string`, required)
  - `reason` (`string`, optional, default `"LOST_AND_REPLACED"`)
- **Response (`200 AuthorizationResult`):**
  - `credential_id`: `replacement_credential_id`
  - `details`: `{ "lost_credential_id", "replacement_credential_id", "user_id", "role", "assigned_floors" }`
- **Common Denials:** `401 UNREGISTERED_CARD` (`lost_credential_id` missing), `403 UNAUTHORIZED_ROLE` / `PEER_ADMIN_REQUIRED`, `409 REPLACEMENT_ID_IN_USE`

---

### 2.4 Fleet Operations & Permissions (`api/acs/fleet.py`)

#### `POST /api/v1/acs/fleet/maintenance`
Authorize a maintenance isolation or diagnostic operation.
- **Auth:** Bearer (`ADMIN` or `TECHNICIAN` — `MAINTENANCE_CONTROL`)
- **Request Body (`MaintenanceOperationRequest`):**
  - `elevator_id` (`ElevatorId | str`, required)
  - `operation_type` (`MaintenanceOperationType | str`, required: `ENTER_MAINTENANCE` | `DIAGNOSTIC_QUERY`)
- **Response (`200 AuthorizationResult`):** `details: { "elevator_id", "operation_type", "role" }`
- **Denials:** `400 MALFORMED_MSG_REJECTED`, `401 SESSION_*`, `403 UNAUTHORIZED_ROLE`

#### `POST /api/v1/acs/fleet/return-to-service`
Authorize returning a car from maintenance to service with mandatory safety confirmation.
- **Auth:** Bearer (`ADMIN` or `TECHNICIAN` — `MAINTENANCE_CONTROL`)
- **Request Body (`ReturnToServiceRequest`):**
  - `elevator_id` (`ElevatorId | str`, required)
  - `technician_confirmation` (`boolean`, required — `false` returns `400 CONFIRMATION_REQUIRED`)
- **Response (`200 AuthorizationResult`):** `details: { "elevator_id", "role" }`

#### `POST /api/v1/acs/fleet/emergency-override`
Authorize an Admin emergency override or incident resolution command.
- **Auth:** Bearer (`ADMIN` only — `EMERGENCY_OVERRIDE`)
- **Request Body (`EmergencyOverrideRequest`):**
  - `override_type` (`EmergencyOverrideType | str`, required: `DOOR_OVERRIDE` | `MANUAL_RECALL` | `RESOLVE_INCIDENT`)
  - `target_car_id` (`ElevatorId | str | null`, optional)
  - `incident_id` (`string | null`, optional)
  - `reason` (`string`, required)
- **Response (`200 AuthorizationResult`):** `details: { "override_type", "target_car_id", "incident_id" }`
- **Denials:** `400 MALFORMED_MSG_REJECTED`, `403 UNAUTHORIZED_ROLE`

#### `POST /api/v1/acs/fleet/rescue-dispatch`
Authorize dispatching an empty residential car (`E1`–`E4`) for rescue.
- **Auth:** Bearer (`ADMIN` only — `EMERGENCY_OVERRIDE`)
- **Request Body (`RescueDispatchRequest`):**
  - `incident_id` (`string`, required)
  - `rescue_car_id` (`ElevatorId | str`, required — `E5` rejected with `409 COMMAND_CONFLICT_REJECTED`)
  - `incident_floor` (`integer`, required)
  - `dwell_wait_seconds` (`integer`, optional, default `120`, must be `1`–`600` or `400 MALFORMED_MSG_REJECTED`)
- **Response (`200 AuthorizationResult`):** `details: { "rescue_car_id", "incident_id", "incident_floor", "dwell_wait_seconds" }`

#### `GET /api/v1/acs/fleet/view`
Verify dashboard permission (`VIEW_FLEET_STATUS`) for viewing fleet status (`ADMIN` or `TECHNICIAN`). Returns `200 AuthorizationResult`.

#### `GET /api/v1/acs/fleet/diagnostics`
Verify dashboard permission (`VIEW_DIAGNOSTICS`) for viewing diagnostics (`ADMIN` or `TECHNICIAN`). Returns `200 AuthorizationResult`.

---

### 2.5 Logs, Alerts, Incidents & Audit Verification (`api/acs/logs.py`)

All search endpoints below return `{ "items": [ ... ], "count": <int> }`, ordered newest-first (`DESC`).

#### `GET /api/v1/acs/logs/authorization-events`
Search `access_authorization_events` (`ADMIN` only — `VIEW_AUDIT_LOGS`).
- **Query Params:** `credential_id` (`str`), `elevator_id` (`ElevatorId | str`), `outcome` (`ALLOWED` | `DENIED`), `denial_reason` (`DenialReason | str`), `start_time` (`datetime`), `end_time` (`datetime`), `limit` (`int`, default `100`, `1`–`10000`).

#### `GET /api/v1/acs/logs/audit`
Search `audit_log_chain` (`ADMIN` only — `VIEW_AUDIT_LOGS`).
- **Query Params:** `actor_id` (`str`), `action_type` (`AuditActionType | str`), `target_id` (`str`), `start_time` (`datetime`), `end_time` (`datetime`), `limit` (`int`, default `100`, `1`–`10000`).

#### `GET /api/v1/acs/logs/incidents`
Search `incidents` (`ADMIN` only — `VIEW_AUDIT_LOGS`).
- **Query Params:** `status` (`str`), `emergency_type` (`str`), `elevator_id` (`ElevatorId | str`), `start_time` (`datetime`), `end_time` (`datetime`), `limit` (`int`, default `100`, `1`–`10000`).

#### `GET /api/v1/acs/logs/alerts`
Search `security_alerts` (`ADMIN` or `TECHNICIAN` — `VIEW_DIAGNOSTICS`).
- **Query Params:** `status` (`AlertStatus | str`: `OPEN` | `ACKNOWLEDGED` | `RESOLVED`), `severity` (`AlertSeverity | str`), `alert_type` (`AlertType | str`), `credential_id` (`str`), `start_time` (`datetime`), `end_time` (`datetime`), `limit` (`int`, default `100`, `1`–`10000`).

#### `PATCH /api/v1/acs/logs/alerts/{alert_id}`
Update a security alert's lifecycle status (`ADMIN` only — `EMERGENCY_OVERRIDE`).
- **Path Param:** `alert_id` (`string`)
- **Request Body (`AlertStatusUpdateRequest`):**
  - `new_status` (`AlertStatus | str`: `OPEN` | `ACKNOWLEDGED` | `RESOLVED`)
- **Responses:**
  - `200`: `{ "updated": true, "alert_id": "...", "new_status": "RESOLVED" }`
  - `400`: `AuthorizationResult` (`MALFORMED_MSG_REJECTED`) on invalid status
  - `403`: `AuthorizationResult` (`UNAUTHORIZED_ROLE`) for non-Admin
  - `404`: `{ "updated": false, "alert_id": "...", "detail": "Security alert not found." }`

#### `POST /api/v1/acs/logs/audit/verify`
Verify linear SHA-256 hash-chain integrity across all rows in `audit_log_chain` (`ADMIN` only — `RUN_AUDIT_INTEGRITY`). Emits `INTEGRITY_CHECK_RUN` when the chain passes.
- **Auth:** Bearer (`ADMIN` only)
- **Response (`200 AuthorizationResult`):**
  - `details`: `{ "passed": bool, "total_records_inspected": int, "mismatch_index": int | null, "reason": str | null }`

---

## 3. Mode 5: Analytics & Reporting (`/api/v1/analytics`)

All Analytics endpoints require an active `ADMIN` Bearer token:
- KPI query routes (`api/analytics/metrics.py`) require `VIEW_ANALYTICS`.
- Export (`api/analytics/export.py`) requires `EXPORT_ANALYTICS_REPORTS`.
- `TECHNICIAN` and unauthorized sessions receive `403 UNAUTHORIZED_ROLE` or `401 SESSION_INVALID` / `SESSION_EXPIRED` with `AuthorizationResult`.

### 3.1 KPI Endpoints (`api/analytics/metrics.py`)

#### `GET /api/v1/analytics/wait-times`
Compute hall-call wait-time statistics over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required), `traffic_profile` (`TrafficProfile | str`, optional).
- **Response (`200 WaitTimeReport`):**
  - `total_calls`, `completed_calls`, `dropped_calls` (`int`)
  - `drop_rate_pct`, `mean_wait_seconds`, `median_wait_seconds`, `p90_wait_seconds`, `p95_wait_seconds`, `min_wait_seconds`, `max_wait_seconds` (`float`)
  - `reassigned_calls_count`, `no_boarding_count` (`int`)
  - `target_met` (`bool` — `PEAK`: `mean <= 60s` and `p95 <= 120s`; `OFF_PEAK`: `mean <= 90s`)

#### `GET /api/v1/analytics/travel-times`
Compute physical cab leg travel-time statistics over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required), `elevator_id` (`ElevatorId | str`, optional).
- **Response (`200 TravelTimeReport`):**
  - `total_trips`, `estimated_trips_count`, `service_runs_count` (`int`)
  - `mean_travel_seconds`, `median_travel_seconds`, `p90_travel_seconds`, `p95_travel_seconds`, `min_travel_seconds`, `max_travel_seconds` (`float`)

#### `GET /api/v1/analytics/trip-distribution`
Compute directional and B1/E5 trip shares over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required), `elevator_id` (`ElevatorId | str`, optional), `traffic_profile` (`TrafficProfile | str`, optional).
- **Response (`200 TripDistributionReport`):**
  - `total_trips`, `up_trips_count`, `down_non_b1_trips_count`, `down_to_b1_trips_count`, `service_runs_count` (`int`)
  - `up_trips_share_pct`, `down_non_b1_trips_share_pct`, `down_to_b1_trips_share_pct`, `service_runs_share_pct` (`float`)

#### `GET /api/v1/analytics/utilization`
Compute per-car wear, distance, and operational state durations over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required), `floor_height_meters` (`float > 0`, optional, default `3.0`).
- **Response (`200 dict[str, CarUtilizationReport]`):** Map keyed by `"E1"`–`"E5"`, each containing:
  - `elevator_id` (`str`), `trip_count`, `motor_starts`, `floors_traversed` (`int`)
  - `distance_meters`, `in_service_seconds`, `fault_seconds`, `maintenance_seconds`, `emergency_seconds`, `occupied_service_seconds`, `stale_seconds` (`float`)

#### `GET /api/v1/analytics/downtime`
Compute per-car downtime, ongoing downtime intervals, and heartbeat-based SREMS host availability over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required), `report_time` (`datetime`, optional — defaults to `SimulationClock.now()`).
- **Response (`200 DowntimeReport`):**
  - `total_downtime_seconds`, `system_availability_pct`, `total_window_seconds`, `unannounced_downtime_seconds`, `announced_maintenance_seconds` (`float`)
  - `per_car_downtime_seconds` (`dict[str, float]`)
  - `ongoing_intervals` (`list[dict]`)
  - `meets_availability_target` (`bool`, `>= 99.5%`)

#### `GET /api/v1/analytics/security`
Summarize authorization denials, failed auth attempts, alerts by type, malformed-message rejections, and emergency incidents over `[start_time, end_time]`.
- **Query Params:** `start_time` (`datetime`, required), `end_time` (`datetime`, required).
- **Response (`200 SecuritySummaryReport`):**
  - `total_denials`, `failed_auth_attempts`, `repeated_failure_alerts`, `cloned_credential_alerts`, `card_scanning_alerts`, `door_obstruction_alerts`, `external_validation_failures`, `total_incidents` (`int`)
  - `denials_by_reason` (`dict[str, int]`)
  - `emergency_incidents_by_type` (`dict[str, int]`)

---

### 3.2 Report Export (`api/analytics/export.py`)

#### `POST /api/v1/analytics/export`
Serialize summary KPIs and optional per-request call/trip rows into `JSON` or `CSV` at `SimulationClock.now()`, emitting a `REPORT_EXPORTED` audit entry.
- **Auth:** Bearer (`ADMIN` only — `EXPORT_ANALYTICS_REPORTS`)
- **Request Body (`AnalyticsExportRequest`):**
  - `run_id` (`string`, required)
  - `export_format` (`"JSON" | "CSV"`, optional, default `"JSON"`)
  - `include_per_request` (`boolean`, optional, default `false`)
  - `start_time` (`datetime | null`, optional — auto-resolved from `hall_calls`/`trips` bounds if omitted)
  - `end_time` (`datetime | null`, optional)
- **Response (`200 ExportPayload`):**
  - `run_id`, `export_format`, `generated_at` (`str`)
  - `record_count` (`int`)
  - `content` (`str` — serialized JSON document or CSV text)
  - `summary` (`dict[str, Any]` containing all 6 KPI reports and `time_window`)
  - `per_request_data` (`list[dict] | null`)

---

## 4. Shared Health, Fleet Stub & Virtual Clock Endpoints (`api/routes.py`, `api/emulator/system.py`)

| Method & Path | Auth | Purpose |
| :--- | :--- | :--- |
| `GET /api/v1/health` | None | Basic liveness check (`status: "ok"`, `service: "srems-api"`). |
| `GET /api/v1/health/db` | None | Executes `SELECT 1;` on `DbConnectionPool` (`200` connected or `503` disconnected). |
| `GET /api/v1/fleet/status` | None | Placeholder operational state stub for `E1`–`E5` (`source: "operational-state-unavailable"`). |
| `GET /api/v1/system/clock` | None | Returns `ClockSnapshot` (`current_sim_time`, `base_time`, `tick`, `sim_seconds_per_tick`, `tick_interval_seconds`, `running`). |
| `POST /api/v1/system/reset` | Bearer (`ADMIN`) | Resets PostgreSQL tables, clears ACS in-memory sessions/windows, and resets `SimulationClock` (`base_time`, `sim_seconds_per_tick`, `tick_interval_seconds`, `start_loop`). |
| `POST /api/v1/system/clock/tick` | Bearer (`ADMIN`) | Manually advances `SimulationClock` by `steps` (`0`–`100000`, default `1`). |

