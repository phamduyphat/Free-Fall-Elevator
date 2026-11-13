# SREMS Mode 5 Specification: Analytics, Audit Management & Reporting (`design_decisions_Mode5.md`)

This document formalizes the architectural specifications, mathematical formulations, database schemas, and OOP module designs for **Mode 5: Analytics, Audit Management, and Reporting** of SREMS based on `SRS.pdf` (v1.1) and established system decisions.

## 1. Architectural Role & Boundary: Passive Analytical Observer

> [!IMPORTANT]
> **Mode 5 Control Authority: Strictly Passive & Read-Only**
> * **No Elevator Control Authority:** Mode 5 does **not** dispatch cars, schedule motion legs, alter queues, or issue movement/door directives across the Elevator Interface (`SRS §3.2.5, §3.3.2`).
> * **Mode 5 Strictly Reads Logs (No Proxy Writing):** Mode 5 is a **read-only consumer** of database tables and audit logs. It **never writes or generates log entries on behalf of other subsystems**:
>   * **Mode 1 (Dispatch):** Writes and updates its own operational requests in `hall_calls`.
>   * **Mode 2 (Access Control):** Writes and manages `credentials`, `access_logs`, and security audit records via `ACS.Logging`.
>   * **Mode 3 (Emergency):** Writes and updates its own emergency incident lifecycles in `incidents`.
>   * **Mode 4 (Maintenance):** Writes its own downtime and diagnostic records in `car_downtime_intervals`.
>   * **Elevator Interface Gateway:** Writes completed physical motion legs into `trips` and availability heartbeats into `system_heartbeats`.
> * **Pure Analytical & Inquiry Scope:** Mode 5 strictly queries these existing tables on-demand via `DbConnectionPool` and `ACS.Logging` to compute KPIs, calculate statistical distributions, verify ledger integrity, and serialize export files. (The only audit event emitted under Mode 5 is `REPORT_EXPORTED`, which is sent to `ACS.Logging` when an Admin triggers an export).

---

## 2. Timing Model & Deterministic Simulation Clock

### 2.1 Dual-Clock Architecture & Test Repeatability (`BEN-2, OR-10`)
To prevent software execution speed (e.g., 1 Hz interactive UI vs. 1,000 Hz headless CI test) from distorting elevator physics and timing KPIs, SREMS strictly separates simulation time from CPU wall-clock time:

1. **Virtual Simulation Clock ($T_{\text{sim}}$):**
   * All elevator motion, wait times, travel times, door dwell timers, 5-second credential authorization windows, 4-hour visitor pass expirations, and traffic profile windows are measured in **virtual simulation time**.
   * Simulation time is anchored to a configurable base datetime ($T_0$) loaded at startup:
     $$T_{\text{sim}} = T_0 + (\text{tick} \times \Delta t_{\text{sim\_seconds}})$$
     * **Morning Peak Profile (`PERF-8`):** $T_0 =$ `2026-01-01 07:00:00` ($\Delta t = 1.0\text{s}$)
     * **Evening Peak Profile (`PERF-8`):** $T_0 =$ `2026-01-01 17:00:00` ($\Delta t = 1.0\text{s}$)
     * **Off-Peak Profile (`PERF-8`):** $T_0 =$ `2026-01-01 22:00:00` ($\Delta t = 1.0\text{s}$)
     * **Default Test Run:** $T_0 =$ `2026-01-01 00:00:00`
   * **Virtual Clock Storage in Database:** All database records store standard ISO-8601 / SQL `TIMESTAMP` values derived from $T_{\text{sim}}$, rather than raw simulation ticks. This directly satisfies SRS filter-by-time-of-day mandates (`ANA-5.2, ANA-5.7, PERF-8`) using native SQL `TIME(...)` and `BETWEEN` operations across peak and off-peak windows. Simulation ticks remain strictly in-memory state within the simulation execution loop.
2. **High-Resolution CPU Wall Clock (`perf_counter`):**
   * Used strictly for software execution performance assertions (`PERF-1, PERF-2, PERF-3`): verifying that SREMS algorithm processing per tick completes within $< 1.0$ real wall-clock second.
3. **Database Test Isolation vs. Production Retention (`DAT-2, BEN-2`):**
   * **Test / Benchmark Mode:** The database is wiped clean prior to executing each test scenario (`VT-01` to `VT-32` and `BEN-1` to `BEN-5`) to guarantee absolute scenario isolation and repeatable metrics.
   * **Production / Live Deployment:** The data store complies with `DAT-2` (12-month persistent retention, append-only tamper-evident audit log).

---

## 3. Core Metric Formulations & KPI Definitions

### 3.1 Wait-Time Metrics (`ANA-5.2, FR-1.18, PERF-9, PERF-10`)
* **Definition:** Elapsed virtual time between the acceptance of a hall call and the arrival of an assigned elevator at the pickup floor (doors opening):
  $$T_{\text{wait}} = t_{\text{doors\_open\_at\_pickup}} - t_{\text{call\_accepted}}$$
* **Reassignment Invariant (`FR-1.18`):** If a hall call is reassigned (due to car fault, emergency, or load ineligibility), the record **retains the original `accepted_at` timestamp** and increments `reassignments_count`. Wait time is never reset by reassignment.
* **Empty Hall Call Invariant (`FR-1.21`):** If an assigned car arrives at the call floor, opens doors, and no passenger boards within 30 seconds (no car call or load change), the request completes and is tagged `no_boarding = TRUE`. Its wait time is still included in wait-time statistics.
* **Statistical Measures Computed (`ANA-5.2`):**
  * **Mean (Average) Wait Time ($\mu_{\text{wait}}$)**
  * **Median Wait Time ($P_{50}$)**
  * **90th Percentile Wait Time ($P_{90}$)**
  * **95th Percentile Wait Time ($P_{95}$)**
* **Cancelled / Dropped Call Exclusion (`FR-1.20, BEN-3`):**  
  Wait-time statistical distributions ($\mu_{\text{wait}}$, $P_{50}$, $P_{90}$, $P_{95}$) strictly aggregate requests that successfully concluded with an elevator arrival (`status = 'COMPLETED'`). Cancelled (`CANCELLED`), failed (`FAILED`), or blocked calls are excluded from wait-time distributions and reported separately as a distinct **Drop / Cancellation Rate Metric** ($\text{Dropped Rate} = \frac{N_{\text{dropped}}}{N_{\text{total}}} \times 100\%$, per `BEN-3`).
* **Target Thresholds (`PERF-9, PERF-10`):**
  * **Peak Profile:** $\mu_{\text{wait}} \le 60\text{ seconds}$, $P_{95} \le 120\text{ seconds}$ (with $\ge 4$ cars active).
  * **Off-Peak Profile:** $\mu_{\text{wait}} \le 90\text{ seconds}$.

### 3.2 Elevator Travel-Time Metrics (`ANA-5.3, FR-1.19`)
* **Decoupled Car Travel Time vs. Passenger Ride Time:**  
  Per SRS Section 2.3, `FR-1.19`, and `ANA-5.3`, SREMS **does not relabel whole-car operating time as passenger ride time**, because passengers can ride along without presenting a card (`SEC-2.21: "riding along with another passenger is not detected and is not treated as a violation"`), and SREMS has no passenger cameras or weight turnstiles.
* **Definition:** Elapsed virtual time of an elevator cab moving between a recorded departure from an origin floor and arrival at the destination floor:
  $$T_{\text{travel}} = t_{\text{arrival\_at\_destination}} - t_{\text{departure\_from\_origin}}$$
* **Missing / Estimated Timestamps (`ANA-5.3`):**  
  If an arrival event lacks a hardware timestamp, SREMS uses its local virtual receipt timestamp and tags the metric with `is_estimated = TRUE`.

### 3.3 Utilization & Mechanical Wear Metrics (`ANA-5.4`)
* **`trip_count`:** Cumulative count of completed physical start-stop cab movement legs.
* **`motor_starts`:** Cumulative count of motor start transitions (1 per discrete motion leg).
* **`floors_traversed`:** Cumulative vertical distance in floors traversed ($|\text{destination} - \text{origin}|$).
* **Nominal Metric Distance Equivalent:**
  $$\text{Distance (Meters)} = \text{floors\_traversed} \times \text{FLOOR\_HEIGHT\_METERS}$$
  *(Configurable parameter, default $3.0\text{ m/floor}$)*.
* **Operational State Duration Distribution (`ANA-5.4` - Derived Calculation):**
  To maintain a lightweight database footprint without a redundant high-frequency state-transition table, state durations are derived directly from existing tables:
  * $\text{Time}_{\text{FAULT}}$, $\text{Time}_{\text{MAINTENANCE}}$, $\text{Time}_{\text{EMERGENCY}}$: Aggregated directly from `car_downtime_intervals` ($\sum \text{duration\_seconds}$).
  * $\text{Time}_{\text{OCCUPIED}}$: Aggregated from `trips` where `elevator_id = 'E5' AND is_service_run = TRUE` (`FR-1.13, VT-14`).
  * $\text{Time}_{\text{STALE}}$: Aggregated from communication fault intervals in `car_downtime_intervals` (`reason = 'COMMUNICATION_LOSS'`).
  * $\text{Time}_{\text{IN\_SERVICE}}$ (Available for ordinary dispatch):
    $$\text{Time}_{\text{IN\_SERVICE}} = T_{\text{report\_window}} - (\text{Time}_{\text{FAULT}} + \text{Time}_{\text{MAINTENANCE}} + \text{Time}_{\text{EMERGENCY}} + \text{Time}_{\text{OCCUPIED}} + \text{Time}_{\text{STALE}})$$

### 3.4 Trip Distribution & Direction Share Metrics (`OR-10, BEN-1, BEN-4`)
To verify benchmark traffic profile assumptions (`OR-10`) and validate passenger traffic distribution across off-peak, peak, and stress scenarios, SREMS partitions all physical elevator movement legs into three disjoint directional/destination categories:
1. **Up Trips ($\text{Trip}_{\text{up}}$):** Motion legs moving upward:
   $$\text{destination\_floor} > \text{origin\_floor}$$
   *(Note: Since B1 is floor $-1$, any upward trip naturally terminates at Floor 1 or above; an up trip is never to B1).*
2. **Down Trips (Excluding B1) ($\text{Trip}_{\text{down\_non\_b1}}$):** Motion legs moving downward that terminate at a regular passenger or common floor above B1:
   $$\text{destination\_floor} < \text{origin\_floor} \quad \text{AND} \quad \text{destination\_floor} \ne -1$$
3. **Down Trips to B1 ($\text{Trip}_{\text{down\_to\_b1}}$):** Downward motion legs terminating at Basement 1:
   $$\text{destination\_floor} = -1 \quad (\text{with } \text{origin\_floor} > -1)$$

* **Statistical Share Formulations:**
  For total recorded physical motion legs $N_{\text{total}} = N_{\text{up}} + N_{\text{down\_non\_b1}} + N_{\text{down\_to\_b1}}$:
  $$\text{Share}_{\text{up}} = \frac{N_{\text{up}}}{N_{\text{total}}} \times 100\%$$
  $$\text{Share}_{\text{down\_non\_b1}} = \frac{N_{\text{down\_non\_b1}}}{N_{\text{total}}} \times 100\%$$
  $$\text{Share}_{\text{down\_to\_b1}} = \frac{N_{\text{down\_to\_b1}}}{N_{\text{total}}} \times 100\%$$
  *(Where $\text{Share}_{\text{up}} + \text{Share}_{\text{down\_non\_b1}} + \text{Share}_{\text{down\_to\_b1}} = 100\%$, returning $0.0\%$ gracefully when $N_{\text{total}} = 0$).*
* **Dedicated Service Run Tracking (`FR-1.13, VT-14`):**
  Independent of general resident traffic shares, the system reports the count and share of dedicated E5 service runs ($N_{\text{service\_runs}}$ where `elevator_id = 'E5' AND is_service_run = TRUE`) to isolate service/freight transport from normal passenger traffic.

### 3.5 Downtime & Availability Metrics (`ANA-5.5, AVL-1`)
* **Car Downtime (`ANA-5.5`):** Cumulative time an elevator is out of ordinary service due to faults, maintenance, or emergency stops:
  $$\text{Downtime}_{\text{car}} = \sum (t_{\text{return\_to\_service}} - t_{\text{fault\_or\_maint\_start}})$$
* **Ongoing Intervals (`ANA-5.5`):** Any downtime interval with `ended_at IS NULL` at query time is tagged as **ongoing**, with current downtime accumulated dynamically up to $T_{\text{report\_time}}$:
  $$\text{Downtime}_{\text{ongoing}} = T_{\text{report\_time}} - t_{\text{fault\_or\_maint\_start}}$$
* **SREMS Software System Availability (`AVL-1`):**
  $$\text{Availability} = \frac{T_{\text{total\_window}} - T_{\text{unannounced\_downtime}} - T_{\text{announced\_maint}}}{T_{\text{total\_window}} - T_{\text{announced\_maint}}} \times 100\% \ge 99.5\%$$
* **Stateless On-Demand DB Query Calculation (Preventing Information Drift):**
  To prevent information drift and synchronization anomalies between in-memory accumulators and database storage across reboots, crashes, and multi-scenario runs, availability and downtime are **never cached as mutable running totals**. Instead, they are computed entirely on-demand via direct SQL queries over user-submitted time ranges (`start_time` to `end_time`):
  1. **Query Heartbeat Stream:** Retrieve all records from `system_heartbeats` where `timestamp BETWEEN start_time AND end_time` ordered by `timestamp ASC`.
  2. **Detect Interruption Gaps:** Iterate through ordered consecutive heartbeat samples $(t_i, t_{i+1})$. Any sample gap satisfying:
     $$\Delta t = t_{i+1} - t_i > 10\text{ seconds}$$
     indicates an offline period where SREMS failed to emit its mandatory 10-second heartbeat.
  3. **Classify via Restart Log (`audit_log_chain`):** For each gap, query the audit ledger within $[t_i, t_{i+1}]$:
     * **Announced Maintenance:** If preceded by a `SYSTEM_SHUTDOWN` event marked with `is_announced_maintenance = TRUE` (or `reason = 'ANNOUNCED_MAINTENANCE'`), the gap duration $(\Delta t - 10\text{s})$ is classified as an **announced maintenance window** and **excluded from downtime** per `AVL-1`.
     * **Unannounced Downtime:** If no announced maintenance record exists (e.g. abrupt crash, freeze, OOM, or unannounced restart per `VT-26`), the gap duration $(\Delta t - 10\text{s})$ is classified as **unplanned software downtime** and charged against the 99.5% availability budget.

### 3.6 Security & Anomaly Summaries (`ANA-5.6`)
Aggregates security counters across configurable time windows:
* Total Authorization Denials (`SEC-2.6`) grouped by denial reason (`UNAUTHORIZED_FLOOR`, `EXPIRED_CARD`, `ONE_TAP_LIMIT`, etc.).
* Failed Authentication Attempts (`SEC-2.7`).
* Repeated-Failure Alerts (Threshold $\ge 3$ failures within 60s).
* Cloned Credential Presentations (Swipe in 2 cars $< 10\text{s}$, `SEC-2.18`).
* Unregistered Card Scanning Alerts ($\ge 11$ unknown cards within 5m, `SEC-2.19, VT-32`).
* External Message Validation Failures (`SEC-2.10, VT-28`).
* Emergency Incident Counts by Type (`FIRE`, `MEDICAL`, `STUCK`, `OBSTRUCTION`, `HELP_CALL`).

---

## 4. Submodule Architecture & OOP Design

Mode 5 provides analytical KPI computation and report generation, while delegating audit search and cryptographic verification directly to `ACS.Logging`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                              Mode 5 Core                               │
├──────────────────────────────────────────┬─────────────────────────────┤
│            Analytics.Metrics             │    Analytics.ExportEngine   │
│           (On-Demand SQL KPIs)           │         (CSV / JSON)        │
└────────────────────┬─────────────────────┴──────────────┬──────────────┘
                     │                                    │
                     │          Delegates Audit           │
                     │          Search & Integrity        │
                     │          ┌─────────────────────────┘
                     ▼          ▼
┌──────────────────────────────────────────┬─────────────────────────────┐
│             DbConnectionPool             │         ACS.Logging         │
│ (trips, hall_calls, car_calls, downtime) │   (Audit Search & Verify)   │
└──────────────────────────────────────────┴─────────────────────────────┘
```

### 4.1 Submodule 1: `Analytics.Metrics`
Provides pure on-demand SQL aggregation queries:
* `get_wait_time_kpis(start_time, end_time, traffic_profile=None) -> WaitTimeReport`:
  Executes indexed SQL queries over `hall_calls` returning total calls, mean, median, P90, P95, and max wait time.
* `get_travel_time_kpis(start_time, end_time, elevator_id=None) -> TravelTimeReport`:
  Executes indexed SQL queries over `trips` returning average and median car travel times, total legs, and count of estimated timestamps.
* `get_trip_distribution(start_time, end_time, elevator_id=None, traffic_profile=None) -> TripDistributionReport`:
  Executes indexed SQL queries over `trips` returning total trips, count/share of upward trips (`destination_floor > origin_floor`), count/share of downward non-B1 trips (`destination_floor < origin_floor AND destination_floor != -1`), count/share of downward B1 trips (`destination_floor = -1`), and count/share of dedicated E5 service runs (`is_service_run = TRUE`) (`OR-10, BEN-1, BEN-4`).
* `get_utilization_kpis(start_time, end_time) -> dict[str, CarUtilizationReport]`:
  Executes indexed SQL queries over `trips` returning per-car `trip_count`, `motor_starts`, `floors_traversed`, estimated meters, and state hours.
* `get_downtime_summary(start_time, end_time) -> DowntimeReport`:
  Executes stateless indexed SQL queries over `car_downtime_intervals`, `system_heartbeats`, and `audit_log_chain` for user-specified range `[start_time, end_time]`. Computes total resolved/ongoing downtime per car (`ANA-5.5`), and evaluates heartbeat gaps and restart logs to return SREMS availability percentage and unplanned software downtime (`AVL-1, VT-26`).
* `get_security_kpis(start_time, end_time) -> SecuritySummaryReport`:
  Summarizes denials, clone alerts, scanning brute-force triggers, and emergency incidents.

### 4.2 Submodule 2: `Analytics.ExportEngine`
Fulfills `ANA-5.9, BEN-4, VT-29`:
* `export_run_report(run_id, export_format: Literal['CSV', 'JSON'], include_per_request: bool) -> ExportPayload`:
  Serializes benchmark or historical run results into standard downloadable formats:
  * **Per-Request Dataset:** Tabular rows of `(call_id, type, origin, dest, created_at, arrived_at, completed_at, wait_seconds, status, reassignments)`.
  * **Aggregate Summary:** Wait time quantiles, trip distribution shares (Up, Down non-B1, Down to B1, Service runs), per-car utilization, downtime, and security metrics (retrieved from `Analytics.Metrics`).
  * Emits a `REPORT_EXPORTED` event to `ACS.Logging`.

### 4.3 Audit Search & Integrity Verification Delegation (`ACS.Logging`)
To eliminate code duplication and maintain single-responsibility boundaries, audit querying and cryptographic tamper verification are handled directly by `ACS.Logging`:
* **Historical Audit Search (`ANA-5.7, SEC-2.14, VT-29`):**  
  Handled via `ACS.Logging.search_audit_records(filters)` and `ACS.Logging.search_incidents(filters)`. Mode 5 does not duplicate this logic; refer to [`design_decisions_ACS.md §3.2`](file:///home/huyn/Documents/VinUni/Sophomore/Software Construction/SREMS/design_decisions_ACS.md#L118-L124).
* **Cryptographic Hash Chain Verification (`ANA-5.8, OR-11, VT-10`):**  
  Handled via `ACS.Logging.verify_audit_chain(start_index, end_index)`. Performs sequential SHA-256 integrity inspection across `audit_log_chain` and reports whether the ledger is intact or identifies the exact first corrupted index. Refer to [`design_decisions_ACS.md §3.2`](file:///home/huyn/Documents/VinUni/Sophomore/Software Construction/SREMS/design_decisions_ACS.md#L118-L124).

---

## 5. Storage Schema & Database Design

To fulfill `DAT-1` (*Stored Data*), `DAT-2` (*12-Month Retention*), `DAT-3` (*Backup & Recovery*), and `ANA-5.1` (*Historical Persistence*), the following operational tables are provisioned via `DbConnectionPool`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        SREMS Relational Schema                         │
├────────────────────────────────────────────────────────────────────────┤
│  users                    credentials              access_logs         │
│  (Cardholders)            (Card tokens)            (Denial tracking)   │
├────────────────────────────────────────────────────────────────────────┤
│  hall_calls               car_calls                trips               │
│  (Hall request states)    (In-cabin destinations)  (Cab motion legs)   │
├────────────────────────────────────────────────────────────────────────┤
│  incidents                car_downtime             system_heartbeats   │
│  (Emergency tracking)     (Fault intervals)        (10s availability)  │
├────────────────────────────────────────────────────────────────────────┤
│  audit_log_chain                                                       │
│  (Cryptographic SHA-256 Ledger - OR-11, VT-10)                         │
└────────────────────────────────────────────────────────────────────────┘
```

### 5.1 `trips` (Cab Physical Movement Legs - `DAT-1, ANA-5.1, ANA-5.4`)
Buffers in-memory during transit and committed via the Elevator Interface API at the completion of each start-stop cycle:

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `trip_id` | `VARCHAR(36)` | PK, UUID | Unique motion leg identifier |
| `elevator_id` | `VARCHAR(5)` | NOT NULL | Car identifier (`E1` to `E5`) |
| `origin_floor` | `INT` | NOT NULL | Floor where doors closed & car departed (-1 for B1, 1 to 30) |
| `destination_floor`| `INT` | NOT NULL | Floor where car stopped & doors opened (-1 for B1, 1 to 30) |
| `departure_time` | `TIMESTAMP` | NOT NULL | Virtual departure timestamp |
| `arrival_time` | `TIMESTAMP` | NOT NULL | Virtual arrival timestamp |
| `travel_time_seconds`| `DOUBLE PRECISION`| NOT NULL | Physical transit duration (`ANA-5.3`) |
| `floors_traversed` | `INT` | NOT NULL | $|\text{destination\_floor} - \text{origin\_floor}|$ (`ANA-5.4`) |
| `motor_starts` | `INT` | NOT NULL, DEFAULT 1 | Acceleration cycles for this leg |
| `traffic_profile` | `VARCHAR(20)` | NOT NULL | Enum: `PEAK`, `OFF_PEAK`, `NORMAL`, `STRESS` (`OR-10, BEN-1`) |
| `is_service_run` | `BOOLEAN` | NOT NULL, DEFAULT 0 | True if leg was an authorized E5 freight/service run (`FR-1.13, VT-14`) |
| `is_estimated` | `BOOLEAN` | NOT NULL, DEFAULT 0 | True if arrival timestamp was locally estimated |

### 5.2 `hall_calls` (Hall Request Lifecycle - `DAT-1, ANA-5.1, FR-1.7, FR-1.18, FR-1.21`)
Tracks passenger hall calls from pickup request outside the cab until elevator arrival/doors opening. Strictly implements **Figure 3**:
$$\mathbf{RECEIVED} \longrightarrow \mathbf{QUEUED} \longrightarrow \mathbf{ASSIGNED} \longrightarrow \mathbf{APPROACHING} \longrightarrow \mathbf{ARRIVED} \longrightarrow \mathbf{COMPLETED}$$
*(Note: Per `FR-1.7`, `COMPLETED` means the assigned car arrived at the requesting floor; it does not track or imply the passenger's subsequent in-car travel).*

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `call_id` | `VARCHAR(36)` | PK, UUID | Unique hall call identifier |
| `origin_floor` | `INT` | NOT NULL | Floor where hall call was pressed (-1 for B1, 1 to 30) |
| `direction` | `VARCHAR(5)` | NOT NULL | Enum: `UP`, `DOWN` |
| `is_freight` | `BOOLEAN` | NOT NULL, DEFAULT 0 | True for freight call directed to E5 (`FR-1.3, FR-1.13`) |
| `assigned_elevator_id`| `VARCHAR(5)`| NULLABLE | Assigned car (`E1`–`E5`; NULLABLE while in `QUEUED`) |
| `status` | `VARCHAR(30)` | NOT NULL | Enum: `RECEIVED`, `QUEUED`, `ASSIGNED`, `APPROACHING`, `ARRIVED`, `COMPLETED`, `CANCELLED`, `FAILED`, `BLOCKED` |
| `created_at` | `TIMESTAMP` | NOT NULL | Virtual acceptance timestamp |
| `assigned_at` | `TIMESTAMP` | NULLABLE | When dispatcher assigned an eligible car |
| `approaching_at` | `TIMESTAMP` | NULLABLE | When assigned car began arrival approach |
| `arrived_at` | `TIMESTAMP` | NULLABLE | When car arrived & doors opened at pickup floor |
| `completed_at` | `TIMESTAMP` | NULLABLE | When pickup concluded and call completed |
| `wait_time_seconds`| `DOUBLE PRECISION`| NULLABLE | Calculated: $t_{\text{arrived}} - t_{\text{created}}$ (Retained across reassignments, `FR-1.18`) |
| `reassignments_count`| `INT` | NOT NULL, DEFAULT 0 | Number of times reassigned due to fault/emergency (`FR-1.18`) |
| `no_boarding` | `BOOLEAN` | NOT NULL, DEFAULT 0 | Set TRUE if 30s dwell elapsed without rider boarding (`FR-1.21`) |
| `cancellation_reason`| `VARCHAR(50)` | NULLABLE | Reason if cancelled by Admin or system (`FR-1.20`) |

### 5.3 `car_calls` (In-Cabin Destination Request Lifecycle - `DAT-1, ANA-5.1, FR-1.8, FR-1.9, FR-1.12`)
Tracks destination selections from inside the elevator cab. Strictly implements **Figure 4**:
$$\mathbf{RECEIVED} \longrightarrow \mathbf{AUTHORIZATION\_CHECK} \longrightarrow \mathbf{QUEUED} \longrightarrow \mathbf{IN\_TRANSIT} \longrightarrow \mathbf{COMPLETED}$$
*(Note: Per `FR-1.8`, car-call states are never conflated with hall-call completion. Transitions from `QUEUED` to `IN_TRANSIT` when SREMS transmits the destination command to the elevator controller across the interface).*

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `call_id` | `VARCHAR(36)` | PK, UUID | Unique car call identifier |
| `elevator_id` | `VARCHAR(5)` | NOT NULL | Car where destination button was pressed (`E1`–`E5`) |
| `destination_floor`| `INT` | NOT NULL | Target floor requested (-1 for B1, 1 to 30) |
| `credential_id` | `VARCHAR(64)` | NULLABLE | Presented card ID context (if swiped per Table 6) |
| `status` | `VARCHAR(30)` | NOT NULL | Enum: `RECEIVED`, `AUTHORIZATION_CHECK`, `QUEUED`, `IN_TRANSIT`, `COMPLETED`, `REJECTED`, `CANCELLED`, `BLOCKED` |
| `created_at` | `TIMESTAMP` | NOT NULL | Virtual button press timestamp |
| `authorized_at` | `TIMESTAMP` | NULLABLE | When authorization passed (enters `QUEUED`) |
| `command_sent_at`| `TIMESTAMP` | NULLABLE | When command dispatched to controller (enters `IN_TRANSIT`) |
| `completed_at` | `TIMESTAMP` | NULLABLE | When car reported arrival at destination floor (`FR-1.12`) |
| `rejection_reason`| `VARCHAR(40)` | NULLABLE | Recorded reason if authorization denied (`SEC-2.6`) |

### 5.4 `car_downtime_intervals` (Availability & Maintenance - `DAT-1, ANA-5.5, MNT-4.1..4.7`)
Tracks every period an elevator is unavailable:

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `interval_id` | `VARCHAR(36)` | PK, UUID | Unique downtime interval identifier |
| `elevator_id` | `VARCHAR(5)` | NOT NULL | Affected elevator (`E1` to `E5`) |
| `state` | `VARCHAR(20)` | NOT NULL | Enum: `MAINTENANCE`, `FAULT`, `EMERGENCY` |
| `reason` | `TEXT` | NOT NULL | Diagnostic code or incident description |
| `started_at` | `TIMESTAMP` | NOT NULL | Virtual timestamp when downtime began |
| `ended_at` | `TIMESTAMP` | **NULLABLE** | Virtual timestamp when car returned to service (**NULL = Ongoing**) |
| `duration_seconds`| `DOUBLE PRECISION`| NULLABLE | Cumulative downtime duration (calculated upon resolution) |
| `technician_id` | `VARCHAR(36)` | NULLABLE, FK $\rightarrow$ `users` | Technician who isolated or cleared the car |

> [!NOTE]
> **Maintenance Lifecycle & SRS Compliance (`MNT-4.3, MNT-4.6, MNT-4.7, ANA-5.5`):**
> * **Startup Commitment:** When a car is placed into maintenance (`MNT-4.3`) or enters a blocking fault (`MNT-4.1`), a row is **immediately inserted into the database** with `started_at = timestamp`, `ended_at = NULL`, and `duration_seconds = NULL`.
> * **Ongoing Calculation (`ANA-5.5`):** While maintenance is in progress, any downtime report dynamically calculates elapsed downtime as $(T_{\text{report\_time}} - \text{started\_at})$ and flags the interval as `is_ongoing = TRUE`.
> * **Post-Maintenance Completion:** Upon passing recovery checks and executing `RETURN_TO_SERVICE` (`MNT-4.6`), the record is updated with `ended_at = timestamp`, the final `duration_seconds`, and confirming `technician_id` (`MNT-4.7`). This workflow conforms 100% to the SRS without any conflict.

### 5.5 `system_heartbeats` (SREMS Availability Ledger - `AVL-1`)
Tracks software host availability sampled every 10 seconds:

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `heartbeat_id` | `BIGINT` | PK, Monotonic | Monotonic sample index |
| `timestamp` | `TIMESTAMP` | NOT NULL | Virtual timestamp |
| `status` | `VARCHAR(10)` | NOT NULL | Enum: `HEALTHY`, `DEGRADED`, `RECOVERING` |
| `active_cars_count`| `INT` | NOT NULL | Number of cars reporting valid state ($0$–$5$) |
| `active_requests_count`| `INT` | NOT NULL | Number of pending requests |

### 5.6 `incidents` (Emergency Incident Tracking - `DAT-1, EMR-3.4, ANA-5.7`)
Tracks building and car emergency lifecycles:

| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `incident_id` | `VARCHAR(36)` | PK, UUID | Unique incident identifier |
| `emergency_type`| `VARCHAR(30)` | NOT NULL | Enum: `FIRE_ALARM`, `POWER_FAILURE`, `MEDICAL_EMERGENCY`, `STUCK_CAR`, `DOOR_OBSTRUCTION`, `COMMUNICATION_LOSS`, `OVERLOAD` |
| `scope` | `VARCHAR(20)` | NOT NULL | Enum: `BUILDING_WIDE`, `CAR_SPECIFIC` |
| `elevator_id` | `VARCHAR(5)` | NULLABLE | Affected elevator ID (if car-specific) |
| `floor` | `INT` | NULLABLE | Floor location of incident |
| `trigger_source`| `VARCHAR(30)` | NOT NULL | Enum: `PASSENGER_HELP`, `PASSENGER_MEDICAL`, `SENSOR_TRIP`, `ADMIN_MANUAL`, `TIMEOUT` |
| `status` | `VARCHAR(20)` | NOT NULL | Enum: `OPEN`, `RESCUE_DISPATCHED`, `RESOLVED`, `FALSE_ALARM` |
| `created_at` | `TIMESTAMP` | NOT NULL | When incident was detected/reported |
| `resolved_at` | `TIMESTAMP` | NULLABLE | When Admin resolved the incident |
| `resolved_by` | `VARCHAR(36)` | NULLABLE, FK $\rightarrow$ `users` | Admin who closed the incident |

> [!NOTE]
> **Why `incidents` Exists Alongside `audit_log_chain` (`DAT-1, DAT-2, DAT-3, EMR-3.4, EMR-3.9`):**
> 1. **Cryptographic Immutability vs. Mutable State Machine:**
>    * `audit_log_chain` is an **append-only linear cryptographic ledger** (`OR-11, VT-10`). Updating rows in-place (e.g. updating an incident's status to `RESOLVED` or setting `resolved_at`) alters raw data and immediately breaks the SHA-256 hash chain of all subsequent records, causing integrity verification (`ANA-5.8`) to fail.
>    * `incidents` is an **operational state table** modeling the lifecycle of active emergency and safety events (`OPEN` $\rightarrow$ `RESCUE_DISPATCHED` $\rightarrow$ `RESOLVED` / `FALSE_ALARM`). It allows fast, indexed queries for the live monitoring dashboard (`SELECT * FROM incidents WHERE status = 'OPEN'`) without scanning the entire cryptographic chain.
> 2. **Explicit SRS Mandate (`DAT-1, DAT-2, DAT-3, ANA-5.7`):**
>    * `DAT-1` explicitly mandates storing both `incidents` and `audit records` as separate persistent data collections.
>    * `DAT-2` and `DAT-3` enforce different operational backup and retention policies: audit records are copied continuously to the backup location as written (`DAT-3`), whereas operational entities (incidents, requests) follow standard 24h backup snapshots.
> 3. **Dual-Write Architecture:**
>    * The `incidents` table maintains the mutable current operational state.
>    * Every discrete state transition on an incident (`INCIDENT_DETECTED`, `RESCUE_CAR_DISPATCHED`, `INCIDENT_RESOLVED`, `FALSE_ALARM_CLOSED`) simultaneously appends an immutable, tamper-evident event into `audit_log_chain`.

---

## 6. Expanded `audit_log_chain` Event Catalog (`ANA-5.8, OR-11`)

The existing 9-column schema in `design_decisions_ACS.md §5.5` is preserved intact. To encompass Mode 5 reporting, system availability (`AVL-1`), and data backup/recovery (`DAT-3`), the following standardized `action_type` events are formalized:

| Subsystem / Domain | `action_type` | Triggering Event & SRS Requirement | Actor | Payload Details |
| :--- | :--- | :--- | :--- | :--- |
| **Mode 5 (Reporting)** | `REPORT_EXPORTED` | Admin exports performance/audit run report (`ANA-5.9, VT-29`) | Admin ID | Report type, date range, export format (`CSV` / `JSON`), row count |
| **Mode 5 (Integrity)** | `INTEGRITY_CHECK_RUN` | Admin runs audit-integrity verification (`ANA-5.8, VT-10`) | Admin ID | Records verified, result (`PASS` / `FAIL`), first mismatched index |
| **Availability (Host)** | `SYSTEM_STARTUP` | SREMS process initialization / boot (`AVL-1, MNT-4.11`) | `"SYSTEM"` | Startup timestamp, version, recovered state snapshot |
| **Availability (Host)** | `SYSTEM_SHUTDOWN` | SREMS clean shutdown or graceful stop (`AVL-1, MNT-4.11`) | Admin / `"SYSTEM"` | Shutdown reason, `is_announced_maintenance` flag (`AVL-1`), final active requests count |
| **Data Protection** | `DATA_BACKUP_CREATED` | Scheduled 24h backup snapshot completed (`DAT-3`) | `"SYSTEM"` | Backup archive path, records backed up, SHA-256 snapshot hash |
| **Data Protection** | `DATA_RESTORE_COMPLETED`| Database restored from backup & chain verified (`DAT-3, VT-25`)| Admin ID | Restored backup timestamp, records restored, verification outcome |

---

## 7. Verification & Test Suite Traceability

| Verification Test | Mode 5 Responsibility | Implemented Mechanism |
| :--- | :--- | :--- |
| **`VT-01` (Hall Call & Dispatch)** | Record request acceptance, assignment, and pickup timestamps | `hall_calls` table lifecycle updates |
| **`VT-05` (Direction-Aware Dispatch)**| Evaluate predicted service times vs. actual recorded wait times | `Analytics.Metrics.get_wait_time_kpis()` |
| **`VT-10` (Tamper-Evidence Check)** | Detect altered/removed records and identify exact first invalid index | `ACS.Logging.verify_audit_chain()` |
| **`VT-14` (Freight Service Run)** | Track dedicated direct service runs and isolate from resident traffic | `trips.is_service_run = TRUE` & `get_trip_distribution()` |
| **`VT-23` (Traffic Profiles)** | Compute wait times stratified by Peak vs. Off-Peak profiles | `Analytics.Metrics.get_wait_time_kpis(traffic_profile)` |
| **`VT-25` (Backup & Restore)** | Verify audit-integrity check passes after database restore | `ACS.Logging.verify_audit_chain()` run post-restore |
| **`VT-26` (SREMS Crash & Recovery)**| Track host availability and ensure no commands issued while down | `system_heartbeats` & `SYSTEM_STARTUP` audit logs |
| **`VT-29` (Search & Export)** | Search audit logs by multi-field filters; export per-request and summary data | `ACS.Logging.search_audit_records()` & `Analytics.ExportEngine` (CSV/JSON) |
| **`OR-10` / `BEN-1` (Profile Splits)**| Validate request distributions and trip direction shares (Up, Down, B1) | `Analytics.Metrics.get_trip_distribution()` |
