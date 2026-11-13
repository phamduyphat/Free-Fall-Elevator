# SREMS Access Control System (ACS) Specification & Data Design (`design_decisions_ACS.md`)

This document formalizes all functional requirements, operational workflows, implementation architecture, and data storage for the **Access Control System (ACS / Mode 2)** of SREMS based on `SRS.pdf` (v1.1) and established design decisions.

---

## 1. Functional Requirements & Role Permission Matrix

### 1.1 Core Identity & Simulated Credentials (`SEC-2.1`)
Every access card is represented by a unique digital credential identifier (`credential_id`) bound to a user identity, role, and permission set. Physical biometric/RFID hardware is simulated.

### 1.2 Role-Based Access Control Matrix (`SEC-2.2, SEC-2.13`)
ACS maintains 5 registered operational roles (the `PASSENGER` role is omitted from ACS credential enums since anonymous passengers do not emit authentication events):
1. `RESIDENT`
2. `VISITOR`
3. `BUILDING_SERVICE`
4. `TECHNICIAN`
5. `ADMIN` (Per SRS Section 1.3, Security Manager and Building Owner share identical system authority and permissions, differing only in client dashboard view focus; both are unified under the canonical `ADMIN` role)

The table below defines the exact capabilities and permissions for each credential type across the system:

| Credential Type (Role) | Access Assigned Floor | Access Common Floors (F1 & B1) | Request E5 Freight Call | Initiate E5 Service Run (`FR-1.13`) | Maintenance & Diagnostics (`MNT-4.3`) | Return Car to Service (`MNT-4.6`) | Manage Credentials & Passes (`SEC-2.16`) | Emergency Override & Rescue (`EMR-3.8`) | View Logs & Security Alerts (`SEC-2.14`) | Run Audit Integrity Check (`ANA-5.8`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`RESIDENT`** | **YES** | **YES** | **YES** | NO | NO | NO | NO | NO | NO | NO |
| **`VISITOR`** | **YES** (Named) | **YES** (F1 & B1) | NO | NO | NO | NO | NO | NO | NO | NO |
| **`BUILDING_SERVICE`** | NO | **YES** (F1 & B1) | **YES** | **YES** | NO | NO | NO | NO | NO | NO |
| **`TECHNICIAN`** | NO | **YES** (F1 & B1) | **YES** | **YES** | **YES** | **YES** | NO | NO | **YES** (Diag) | NO |
| **`ADMIN`** | **YES** (All) | **YES** (F1 & B1) | **YES** | **YES** | **YES** | **YES** | **YES** | **YES** | **YES** | **YES** |

* **Visitor Floor Policy (`SEC-2.22`):** A visitor pass authorizes travel between the visitor's allowed host resident floor(s), **Floor 1 (Lobby)**, and **Basement 1 (B1)**.
* **Unauthenticated Exceptions (`SEC-2.4, SEC-2.5`):**
  * **Hall Calls:** Submitting Up/Down hall calls requires **no card** (`SEC-2.4`).
  * **Basement 1 (B1):** Selecting B1 inside any residential car (`E1`–`E4`) requires **no card** (`SEC-2.5`).
* **Floor 1 (Lobby) Policy:** Selecting Floor 1 requires an authorized card tap (granted by default to all registered credentials).

### 1.3 Destination Gating & Presentation Rules
* **Pre-Dispatch Gate (`SEC-2.6`):** Unauthorized destination button presses are intercepted and blocked **before** any command reaches the elevator controller.
* **Single-Use Tap Rule & 5-Second Button Window (`SEC-2.21`):**
  * One credential presentation authorizes **exactly one** restricted destination selection.
  * **5-Second Selection Window:** Once tapped, the passenger has **5 seconds** to press a destination button; if no button is pressed within 5 seconds, the authorization token expires and resets.
  * **Token Override / Preemption:** If another credential is swiped in the same elevator before a button is pressed or before the 5 seconds expire, the active token is immediately overridden by the new presentation, granting a fresh 5-second selection window for the new card ID.
  * Subsequent button presses require re-tapping the card. Riding along to an already-selected floor is permitted and not flagged as a violation.
* **Sequential Command Execution (`SEC-2.20`):** Commands acting on the same elevator must be queued and executed in strict FIFO arrival order.

### 1.4 Credential Lifecycle & Anomaly Detection
* **Lifecycle State Machine (`SEC-2.16`):**
  $$\mathbf{ACTIVE} \longleftrightarrow \mathbf{SUSPENDED} \longrightarrow \mathbf{REVOKED} \quad \text{or} \quad \mathbf{ACTIVE} \longrightarrow \mathbf{EXPIRED}$$
* **Lost Card Workflow (`SEC-2.17`):** Reporting a card lost immediately sets it to `SUSPENDED` (reason `LOST`) for a **72-hour review window** (configurable 48–72h). If not found within 72h, it auto-transitions to `REVOKED`. Tapping a suspended or revoked card triggers an immediate security alert on the first attempt.
* **Cloned Credential Detection (`SEC-2.18`):** If the same credential ID is tapped in two different elevator cabs within **$< 10$ seconds**, the second request is denied, the credential is immediately set to `SUSPENDED` (`reason: CLONE_SUSPECTED`), and a critical alert naming both cars is generated.
* **Unregistered Credential & Scanning Abuse (`SEC-2.19, VT-32`):**
  * Tapping an unknown card ID is denied and logged.
  * 3 failed attempts within 60s for the same ID raises a security alert (`SEC-2.7`).
  * $\ge 11$ distinct unregistered card IDs presented within 5 minutes triggers a **Card Scanning (Brute-Force) Alert** (`VT-32`).
* **Visitor Pass Lifecycle (`SEC-2.22, VT-31`):** Time-limited pass limited to named floors. Automatically transitions to `EXPIRED` after a set time (**default 4 hours**).
* **Admin Peer-Administration & Accountability (`SEC-2.17, Section 2.5`):** In accordance with SRS Section 2.5 ("At least two Admins are available at all times or Admins must have backup cards"), admin recovery is strictly governed by **peer administration**. SREMS rejects untraceable shared master keys because an anonymous master key breaks cryptographic non-repudiation and cannot attribute actions to a specific human actor (`SEC-2.11`). For initial system deployment, the database is seeded with two initial Admin credentials via migration/setup script. In production, if an Admin card is lost, another Admin must authenticate and execute the replacement (`SEC-2.17`).

---

## 2. Operations Breakdown: Passenger-Side vs. Admin-Side

```
┌────────────────────────────────────────────────────────┐   ┌────────────────────────────────────────────────────────┐
│               PASSENGER-SIDE OPERATIONS                │   │                 ADMIN-SIDE OPERATIONS                  │
│       (Car Operating Panel [UI-8] & Hall Panel)        │   │          (Monitoring Dashboard [UI-3, UI-10])          │
├────────────────────────────────────────────────────────┤   ├────────────────────────────────────────────────────────┤
│ • Place Hall Call (No authentication required)         │   │ • Admin Dashboard Login (Inactivity timeout: 15 mins)  │
│ • Select B1 Destination (No authentication required)   │   │ • Issue New Resident / Staff / Technician Credential   │
│ • Tap Credential at in-cabin reader                    │   │ • Issue Visitor Pass (Allowed floors, F1 & 4h expiry)  │
│ • Select Destination Floor (Within 5-second window)    │   │ • Suspend Lost Card (Initiates 72-hour review window)  │
│ • Receive Audio/Visual Feedback:                       │   │ • Reinstate Suspended Card (Identity verified)         │
│   - Access Granted (Button illuminates, floor queued)  │   │ • Revoke & Re-issue Lost Card (Atomic single-click)    │
│   - Access Denied (Buzz/chime, denial message, reason) │   │ • Search & Filter Security Logs & Denials [SEC-2.14]   │
│ • Trigger HELP or MEDICAL button (No card required)    │   │ • Run Cryptographic Audit Integrity Check [ANA-5.8]    │
│                                                        │   │ • Acknowledge & Resolve Security Alerts                │
│                                                        │   │ • Issue Privileged Emergency Overrides & Rescue Cars   │
└────────────────────────────────────────────────────────┘   └────────────────────────────────────────────────────────┘
```

---

## 3. Submodule Architecture: `ACS.AccessControl` & `ACS.Logging`

The ACS is decoupled into two specialized submodules, each implemented as **OOP classes with static properties and static methods** communicating with a separately implemented database connection pool (`DbConnectionPool`):

```
                        ┌──────────────────────────────────────────┐
                        │              SREMS Core Loop             │
                        └──────────────┬───────────────────────────┘
                                       │ Calls static methods
                                       ▼
       ┌────────────────────────────────────────────────────────────────┐
       │                      ACS.AccessControl                         │
       │   (Validates Payloads, Enforces Policies, Manages Tokens)      │
       └──────────────┬──────────────────────────────────┬──────────────┘
                      │                                  │
          Queries/    │                                  │ Invokes logging
          Validates   ▼                                  ▼
 ┌─────────────────────────────┐        ┌───────────────────────────────┐
 │      DbConnectionPool       │        │          ACS.Logging          │
 │   (Implemented Separately)  │        │ (Hash-Chaining, Alerts, Logs) │
 └─────────────────────────────┘        └──────────────┬────────────────┘
                                                       │ Writes audit records
                                                       ▼
                                        ┌───────────────────────────────┐
                                        │       DbConnectionPool        │
                                        └───────────────────────────────┘
```

### 3.1 Submodule 1: `ACS.AccessControl`
* **Responsibilities:**
  * Ingests strongly typed request payloads for every security-sensitive operation.
  * Validates credentials, roles, floor access lists, expiration timestamps, and single-use tap tokens.
  * Enforces fail-closed policy (`MNT-4.11`): if database connection pool queries fail, all restricted requests are denied.
  * Emits validation verdicts (`AuthorizationResult`: `ALLOWED` or `DENIED` with detailed reason codes).
  * Automatically invokes `ACS.Logging` for every decision, denial, alert, and state transition.

### 3.2 Submodule 2: `ACS.Logging`
* **Responsibilities:**
  * Centralizes all persistence for authorization decisions, denials, and security alerts.
  * Maintains the **Cryptographic Audit Hash Chain (`audit_log_chain`)** using linear SHA-256 linking (`OR-11, SEC-2.12`).
  * Implements sliding-window anomaly detectors (repeated failed attempts, cloned card presentations, unregistered scanning).
  * Exposes static inquiry methods for Admin security searches (`SEC-2.14`) and audit tamper-verification (`ANA-5.8, VT-10`).

---

## 4. `ACS.AccessControl` Payload Type Specifications

All operations submitted to `ACS.AccessControl` for validation are structured as strongly typed OOP payload classes. In-cabin button operations are decoupled from credential authentication via a two-stage **Swipe $\rightarrow$ Select** flow:

### 4.1 `CardSwipePayload` (Credential Presentation on In-Cabin Reader)
Submitted when a card is presented at an elevator's in-cabin reader (`UI-8`):
* `elevator_id: str` (`E1` through `E5`)
* `credential_id: str` (Unique card identifier presented)
* `timestamp: int` (Simulated tick timestamp / epoch ms)
* **Validation & Session Generation Logic:**
  1. **Cloned Card Check (`SEC-2.18`):** Check in-memory `recent_presentations_window`. If `credential_id` was swiped in a different car within $< 10\text{s}$, deny request, set card status to `SUSPENDED` (`reason: CLONE_SUSPECTED`), generate critical security alert, and record audit event.
  2. **Credential Registration & Status (`SEC-2.16, SEC-2.19`):** Query `credentials` via `DbConnectionPool`. If unregistered, reject with `UNREGISTERED_CARD` and increment scan counter. If status is `SUSPENDED`, `REVOKED`, or `EXPIRED`, reject and alert on first attempt (`SEC-2.17`).
  3. **Role & Floor Extraction:** Extract user role and `allowed_floors` list.
  4. **Generate Per-Car Session Token:** Overwrite any existing token for `elevator_id` in `active_car_sessions`:
     ```python
     # Role-dependent expiration window:
     timeout_seconds = 30 if role in ["BUILDING_SERVICE", "TECHNICIAN"] else 5

     active_car_sessions[elevator_id] = {
         "session_token": generate_uuid(),
         "credential_id": credential_id,
         "role": role,
         "allowed_floors": allowed_floors,
         "granted_tick": current_tick,
         "expires_at_tick": current_tick + (timeout_seconds * ticks_per_sec),
         "consumed": False
     }
     ```
  5. Return `ALLOWED` (car panel illuminates reader feedback).

### 4.2 `CarDestinationRequestPayload` (Passenger Floor Button Press)
Submitted when a passenger presses a physical/virtual floor button inside an elevator cab:
* `elevator_id: str` (`E1` through `E5`)
* `origin_floor: int` (Current floor of the car)
* `requested_floor: int` (Target destination floor: -1 for B1, 1 to 30)
* `timestamp: int`
* **Validation Logic:**
  1. **B1 Universal Exemption (`SEC-2.5`):** If `requested_floor == -1` (B1):
     * If `elevator_id == "E5"`, reject with `E5_B1_FORBIDDEN` (E5 never serves B1, `FR-1.10`).
     * Otherwise, return `ALLOWED` immediately without checking any session token.
  2. **Session Token Lookup:** Inspect `active_car_sessions[elevator_id]`:
     * If no active session exists or `consumed == True` $\rightarrow$ reject with `NO_ACTIVE_CREDENTIAL` (`SEC-2.6`).
     * If `current_tick > expires_at_tick` $\rightarrow$ reject with `TOKEN_WINDOW_EXPIRED` (`SEC-2.21`).
  3. **Floor Authorization Check:** Check if `requested_floor` is in `session.allowed_floors`:
     * If not authorized $\rightarrow$ reject with `UNAUTHORIZED_FLOOR` and log denial with the active `session.credential_id`.
  4. **Consume Token (`SEC-2.21`):** Invalidate/consume the session token (enforcing "one presentation, one destination").
  5. Return `ALLOWED` and dispatch destination directive to the elevator controller.

### 4.3 `E5ServiceRunPayload` (Dedicated Freight Run - `FR-1.13`)
Submitted when building-service staff or technicians press the dedicated service run button inside `E5`:
* `elevator_id: str` (Must be `E5`)
* `destination_floor: int` (1 to 30)
* `purpose: str` (e.g., `"FREIGHT"`, `"WASTE_TRANSPORT"`, `"MAINTENANCE_RUN"`)
* `timestamp: int`
* **Validation Logic:**
  1. Confirm `elevator_id == "E5"`.
  2. Inspect `active_car_sessions["E5"]`: verify active session exists, is unexpired, and belongs to role `BUILDING_SERVICE`, `TECHNICIAN`, or `ADMIN`.
  3. Verify E5 is not currently occupied by another service run (`FR-1.13`).
  4. Consume session token and return `ALLOWED`. Log `E5_SERVICE_RUN_START` to audit log.

### 4.4 `MaintenanceOperationPayload` (Technician Isolation & Diagnostics - `MNT-4.3`)
Submitted when putting a car into maintenance mode or accessing diagnostics:
* `credential_id: str`
* `elevator_id: str` (`E1` to `E5`)
* `operation_type: str` (Enum: `ENTER_MAINTENANCE`, `DIAGNOSTIC_QUERY`)
* `timestamp: int`
* **Validation Logic:**
  1. Query user role. Verify role is `TECHNICIAN` or `ADMIN`.
  2. Verify command sequencing: car is not in active emergency recall (`SEC-2.20`).
  3. Return `ALLOWED` and write audit record.

### 4.5 `ReturnToServicePayload` (Restoring Elevator to Service - `MNT-4.6`)
Submitted when taking a car out of maintenance or fault mode:
* `credential_id: str`
* `elevator_id: str`
* `technician_confirmation: bool` (Confirms safety checks passed)
* `timestamp: int`
* **Validation Logic:**
  1. Query user role. Verify role is `TECHNICIAN` or `ADMIN`.
  2. Verify car state reports no active blocking faults, fresh snapshot, and unblocked doors (`MNT-4.6`).
  3. Return `ALLOWED` and log maintenance resolution.

### 4.6 `EmergencyOverridePayload` (Admin Safety Overrides - `EMR-3.8`)
Submitted when an Admin executes a manual emergency command or resolves an incident:
* `credential_id: str`
* `override_type: str` (Enum: `DOOR_OVERRIDE`, `MANUAL_RECALL`, `RESOLVE_INCIDENT`)
* `target_car_id: Optional[str]`
* `incident_id: Optional[str]`
* `reason: str`
* `timestamp: int`
* **Validation Logic:**
  1. Query user role. Strictly enforce role is `ADMIN` (`EMR-3.8`).
  2. Validate override conforms to active emergency policy priority (`EMR-3.13`).
  3. Return `ALLOWED` and record in tamper-evident audit log.

### 4.7 `RescueCarDispatchPayload` (Rescue Car Dispatch - `EMR-3.12, EMR-3.16`)
Submitted when an Admin dispatches a rescue car to a stuck car or for a medical emergency:
* `credential_id: str`
* `incident_id: str`
* `rescue_car_id: str` (Must be an eligible residential car `E1`–`E4`)
* `incident_floor: int`
* `dwell_wait_seconds: int` (Default: 120s, max 600s, `EMR-3.16`)
* `timestamp: int`
* **Validation Logic:**
  1. Verify role is `ADMIN` (`EMR-3.12`).
  2. Verify `rescue_car_id != "E5"` (E5 is never a rescue car, `EMR-3.12, OR-15`).
  3. Verify rescue car is empty (load == 0 kg or Admin confirmed empty).
  4. Verify rescue car is not in maintenance or emergency itself.
  5. Return `ALLOWED` and trigger requeuing of rescue car's existing ordinary assignments.

### 4.8 `CredentialManagementPayload` (Admin Credential Lifecycle Operations)
Submitted when an Admin performs card management actions:
* `admin_credential_id: str`
* `action: str` (Enum: `ISSUE`, `SUSPEND`, `REINSTATE`, `REVOKE`, `ISSUE_VISITOR_PASS`)
* `target_user_id: Optional[str]`
* `target_credential_id: Optional[str]`
* `payload_details: dict` (e.g., assigned floors, visitor pass expiry, suspension reason)
* `timestamp: int`
* **Validation Logic:**
  1. Verify role is `ADMIN` (`SEC-2.16`).
  2. If suspending a lost Admin card, strictly enforce `admin_credential_id != target_credential_id` (a lost Admin card must be suspended by another Admin, `SEC-2.17`).
  3. Validate state transitions adhere to lifecycle state machine (`SEC-2.16`).
  4. Return `ALLOWED` and write audit record.

### 4.9 `RevokeAndReissuePayload` (Atomic Lost Card Replacement - `SEC-2.17, VT-17`)
Submitted when an Admin executes the dedicated "Revoke & Re-issue" workflow on the Central Management Panel (`UI-3`) for a lost card:
* `admin_credential_id: str` (The authorizing Admin performing the operation)
* `lost_credential_id: str` (The card identifier being revoked)
* `replacement_credential_id: str` (The unassigned new physical card identifier being assigned)
* `reason: str` (Default: `"LOST_AND_REPLACED"`)
* `timestamp: int`
* **Static Wrapper Function:** `ACS.AccessControl.replaceLostCard(payload: RevokeAndReissuePayload, db_pool: DbConnectionPool) -> AuthorizationResult`
* **Atomic Transaction Logic:**
  1. **Admin Authorization & Peer Check (`SEC-2.17`):** Query `admin_credential_id` from DB; verify role is `ADMIN`. If `lost_credential_id` belongs to an Admin user, strictly verify `admin_credential_id != lost_credential_id` (an admin cannot self-reissue a lost admin card; another peer admin must authorize it).
  2. **Replacement Availability Check:** Verify `replacement_credential_id` is unique and does not already exist as an active credential in `credentials`.
  3. **Atomic ACID Transaction Execution:** Within a single database transaction via `DbConnectionPool`:
     * Fetch existing user details and permissions (`user_id`, `role`, `assigned_floors`) associated with `lost_credential_id`.
     * Update `lost_credential_id`: set `status = 'REVOKED'`, `revocation_reason = payload.reason`, `revoked_at = timestamp`.
     * Insert `replacement_credential_id`: copy `user_id`, copy identical `role`, copy identical `assigned_floors`, set `status = 'ACTIVE'`, `created_at = timestamp`.
  4. **Audit Logging & Non-Repudiation:** Automatically invoke `ACS.Logging.write_audit(...)` with `action_type = 'CREDENTIAL_REPLACED'`, recording `actor_id = payload.admin_credential_id`, `target_id = payload.replacement_credential_id`, and payload `{ lost_credential_id, replacement_credential_id, user_id, role, assigned_floors }`.
  5. Return `ALLOWED` verdict with success summary.

---

## 5. Storage Schema & Required Data Fields

To satisfy `DAT-1` (persistence), `DAT-2` (12-month retention), `DAT-4` (role access), and `SEC-2.12` (tamper protection), the Access Control System interacts with the following database tables via `DbConnectionPool`:

### 5.1 `users` (Cardholders & Operators)
| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `user_id` | `VARCHAR(36)` | PK, UUID | Unique user identifier |
| `full_name` | `VARCHAR(100)` | NOT NULL | Name of resident, staff, technician, or admin |
| `role` | `VARCHAR(30)` | NOT NULL | Enum: `RESIDENT`, `VISITOR`, `BUILDING_SERVICE`, `TECHNICIAN`, `ADMIN` |
| `apartment_unit` | `VARCHAR(10)` | NULLABLE | E.g., "Unit 1402" (mandatory for residents) |
| `assigned_floor`| `INT` | NULLABLE | Primary residential apartment floor (e.g., 14) |
| `dashboard_focus`| `VARCHAR(20)` | NULLABLE | Enum: `SECURITY`, `OPERATIONS` (UI landing tab focus for Admins per SRS Section 1.3) |
| `created_at` | `TIMESTAMP` | NOT NULL | Creation timestamp |
| `updated_at` | `TIMESTAMP` | NOT NULL | Last update timestamp |

### 5.2 `credentials` (Cards & Passes)
| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `credential_id` | `VARCHAR(64)` | PK, Unique | Hex/String card ID (e.g. simulated RFID UID) |
| `user_id` | `VARCHAR(36)` | FK $\rightarrow$ `users.user_id` | Associated cardholder |
| `credential_type`| `VARCHAR(20)` | NOT NULL | Enum: `RESIDENT`, `VISITOR`, `STAFF`, `TECHNICIAN`, `ADMIN` |
| `status` | `VARCHAR(20)` | NOT NULL | Enum: `ACTIVE`, `SUSPENDED`, `REVOKED`, `EXPIRED` |
| `suspension_reason` | `VARCHAR(30)` | NULLABLE | Enum: `LOST`, `CLONE_SUSPECTED`, `ADMIN_MANUAL` |
| `suspension_start` | `TIMESTAMP` | NULLABLE | When card was suspended (for 72h auto-revocation) |
| `expires_at` | `TIMESTAMP` | NULLABLE | Fixed expiration (mandatory for visitor passes, default +4h) |
| `allowed_floors` | `JSON / TEXT` | NOT NULL | Array of permitted floors, e.g. `[1, 14]` |
| `issued_by_admin`| `VARCHAR(36)` | FK $\rightarrow$ `users.user_id` | Admin who authorized issuance |
| `created_at` | `TIMESTAMP` | NOT NULL | Timestamp of issue |
| `updated_at` | `TIMESTAMP` | NOT NULL | Timestamp of state transition |

### 5.3 `access_authorization_events` (Operational Decisions & Denials)
Every credential presentation and destination authorization attempt is captured here:
| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `event_id` | `VARCHAR(36)` | PK, UUID | Unique decision record ID |
| `timestamp` | `TIMESTAMP` | NOT NULL, Indexed | Event time (precision: milliseconds) |
| `credential_id` | `VARCHAR(64)` | NULLABLE, Indexed | Card ID used (null for unauthenticated B1) |
| `elevator_id` | `VARCHAR(5)` | NOT NULL | Car identifier (`E1` through `E5`) |
| `origin_floor` | `INT` | NOT NULL | Floor where the passenger entered / tapped |
| `destination_floor`| `INT` | NOT NULL | Floor requested by passenger (-1 for B1, 1–30) |
| `outcome` | `VARCHAR(10)` | NOT NULL | Enum: `ALLOWED`, `DENIED` |
| `denial_reason` | `VARCHAR(40)` | NULLABLE | Enum: `UNREGISTERED_CARD`, `CARD_SUSPENDED`, `CARD_REVOKED`, `CARD_EXPIRED`, `UNAUTHORIZED_FLOOR`, `E5_B1_FORBIDDEN`, `ONE_TAP_LIMIT_EXCEEDED`, `TOKEN_WINDOW_EXPIRED`, `DB_FAIL_CLOSED` |
| `alert_triggered` | `BOOLEAN` | NOT NULL | Whether this event contributed to/triggered an alert |

### 5.4 `security_alerts` (Security Incident Tracking - `SEC-2.8`)
| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `alert_id` | `VARCHAR(36)` | PK, UUID | Unique alert identifier |
| `severity` | `VARCHAR(15)` | NOT NULL | Enum: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `alert_type` | `VARCHAR(40)` | NOT NULL | Enum: `REPEATED_FAILURES`, `CLONED_CREDENTIAL`, `CARD_SCANNING`, `LOST_CARD_USAGE`, `REVOKED_CARD_USAGE`, `DOOR_OBSTRUCTION` |
| `credential_id` | `VARCHAR(64)` | NULLABLE | Relevant card ID (if identified) |
| `primary_car_id` | `VARCHAR(5)` | NULLABLE | Car where event occurred |
| `secondary_car_id`| `VARCHAR(5)` | NULLABLE | Secondary car ID (specifically for cloned card detection) |
| `details` | `TEXT` | NOT NULL | Diagnostic explanation and context |
| `status` | `VARCHAR(20)` | NOT NULL | Enum: `OPEN`, `ACKNOWLEDGED`, `RESOLVED` |
| `resolved_by` | `VARCHAR(36)` | FK $\rightarrow$ `users.user_id` | Admin who resolved the incident |
| `created_at` | `TIMESTAMP` | NOT NULL | Alert trigger timestamp |
| `resolved_at` | `TIMESTAMP` | NULLABLE | Incident resolution timestamp |

### 5.5 `audit_log_chain` (Immutable Tamper-Evident Ledger - `SEC-2.11, SEC-2.12, ANA-5.8`)
Linear cryptographic hash chain implementing `OR-11` and `VT-10`:
| Field Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `log_index` | `BIGINT` | PK, Monotonic | Sequential record number: 1, 2, 3... |
| `timestamp` | `TIMESTAMP` | NOT NULL | Exact time of logged action |
| `actor_id` | `VARCHAR(36)` | NOT NULL | Admin user ID, Card ID, or `"SYSTEM"` |
| `actor_role` | `VARCHAR(30)` | NOT NULL | Role executing the action (or `"SYSTEM"`) |
| `action_type` | `VARCHAR(50)` | NOT NULL | Standardized event enum (see Section 5.5.1 below) |
| `target_id` | `VARCHAR(64)` | NOT NULL | Identifier of affected entity (card, elevator, incident, etc.) |
| `event_payload` | `TEXT / JSON` | NOT NULL | Serialized details and parameters of the state transition |
| `prev_hash` | `CHAR(64)` | NOT NULL | SHA-256 hash of record `(log_index - 1)` (Genesis: 64 zeros) |
| `current_hash` | `CHAR(64)` | NOT NULL | `SHA-256(log_index || timestamp || actor_id || action_type || event_payload || prev_hash)` |

> [!NOTE]
> **Actor Identification Policy:**
> * **Human-Initiated Actions:** `actor_id` MUST be populated with the authenticated human user's ID (`admin_credential_id`, `technician_id`, or passenger `credential_id`) to maintain strict non-repudiation.
> * **Automated / Autonomous Actions:** To keep implementation minimal without subsystem overhead (per SRS scope), `actor_id` and `actor_role` are strictly set to `"SYSTEM"` for events executed by internal state machines, timers, or ingress firewalls (e.g., 72h window expirations, 4h visitor pass timeouts, clone detection auto-suspensions, rescue car dwell timeouts, and malformed packet drops).

#### 5.5.1 Comprehensive Event Catalog for `audit_log_chain`
The SRS mandates that all security-sensitive operations, privileged state changes, and critical anomalies across **all SREMS subsystems** be written to the tamper-evident `audit_log_chain` via `ACS.Logging`. These include:

| Subsystem / Domain | `action_type` | Triggering Event & SRS Requirement | Actor | Payload Details |
| :--- | :--- | :--- | :--- | :--- |
| **ACS (Authentication)** | `ADMIN_LOGIN` | Admin signs in to dashboard (`SEC-2.15`) | Admin ID | Session ID, client IP / interface |
| **ACS (Authentication)** | `ADMIN_LOGOUT` | Admin logs out or session times out after 15m (`SEC-2.15`) | Admin ID / System | Reason (`MANUAL`, `TIMEOUT`) |
| **ACS (Destination)** | `ACCESS_DENIAL` | Unauthorized destination request blocked (`SEC-2.6, VT-02`) | Card ID / System | Elevator ID, origin, requested floor, denial reason |
| **ACS (Destination)** | `ONE_TAP_VIOLATION` | Second destination selected on single card tap (`SEC-2.21, VT-31`) | Card ID | Elevator ID, denied floor, token age |
| **ACS (Credential)** | `CREDENTIAL_ISSUED` | Admin issues new card to user (`SEC-2.16`) | Admin ID | User ID, credential ID, role, allowed floors |
| **ACS (Credential)** | `CREDENTIAL_SUSPENDED` | Lost card or clone suspected (`SEC-2.16, SEC-2.17, SEC-2.18`) | Admin ID / System | Credential ID, reason (`LOST`, `CLONE_SUSPECTED`), window |
| **ACS (Credential)** | `CREDENTIAL_REINSTATED`| Suspended card confirmed found & reinstated (`SEC-2.17, VT-17`) | Admin ID | Credential ID, cardholder identity confirmation notes |
| **ACS (Credential)** | `CREDENTIAL_REVOKED` | 72h window ends or card permanently revoked (`SEC-2.16, SEC-2.17`)| Admin ID / System | Credential ID, trigger (`WINDOW_EXPIRED`, `ADMIN_ACTION`) |
| **ACS (Credential)** | `CREDENTIAL_REPLACED` | Replacement card issued for lost card (`SEC-2.17, VT-17`) | Admin ID | Old credential ID, new credential ID, user ID |
| **ACS (Visitor Pass)** | `VISITOR_PASS_ISSUED` | Admin issues temporary visitor pass (`SEC-2.22, VT-31`) | Admin ID | Credential ID, allowed floors, expires_at timestamp |
| **ACS (Visitor Pass)** | `VISITOR_PASS_EXPIRED`| Visitor pass 4h expiration reached (`SEC-2.22, VT-31`) | System | Credential ID, total valid duration, final status |
| **ACS (Policy)** | `POLICY_CHANGED` | Admin updates floor permissions or common floors (`SEC-2.9`) | Admin ID | Config key, old value, new value |
| **Mode 1 (Dispatch)** | `CALL_CANCELLED_BY_ADMIN` | Admin cancels active hall or car call (`FR-1.20, VT-16`) | Admin ID | Request ID, elevator ID, floor, recorded reason |
| **Mode 1 (Freight)** | `E5_SERVICE_RUN_START` | Authorized staff/technician starts E5 non-stop run (`FR-1.13, VT-14`)| Staff / Tech ID | Destination floor, purpose (`FREIGHT`, `WASTE`, etc.) |
| **Mode 1 (Freight)** | `E5_SERVICE_RUN_END` | E5 service run completes or released (`FR-1.13, ANA-5.1`) | Staff / Tech ID | Elevator ID, destination arrived, trip duration |
| **Mode 3 (Emergency)** | `EMERGENCY_OVERRIDE` | Admin issues door override or manual recall (`EMR-3.8, VT-13`) | Admin ID | Target elevator ID, override command, rationale |
| **Mode 3 (Emergency)** | `RESCUE_CAR_DISPATCHED`| Admin dispatches empty rescue car to incident (`EMR-3.12, VT-13`) | Admin ID | Rescue car ID, incident ID, target floor, wait time |
| **Mode 3 (Emergency)** | `RESCUE_CAR_BOARDED` | Admin confirms ill person/occupants aboard rescue car (`EMR-3.16`)| Admin ID | Rescue car ID, incident ID, non-stop responder trip |
| **Mode 3 (Emergency)** | `RESCUE_CAR_RELEASED` | Rescue car dwell wait expires or cancelled (`EMR-3.16, VT-13`) | Admin ID / System | Rescue car ID, incident ID, release reason |
| **Mode 3 (Emergency)** | `INCIDENT_RESOLVED` | Authorized staff resolves active emergency incident (`EMR-3.9`) | Admin ID | Incident ID, emergency type, resolution notes |
| **Mode 3 (Emergency)** | `FALSE_ALARM_CLOSED` | Admin closes repeated help triggers as false alarm (`EMR-3.15, VT-21`)| Admin ID | Elevator ID, incident ID, trigger count |
| **Mode 4 (Maintenance)**| `MAINTENANCE_ENTER` | Technician places car into maintenance mode (`MNT-4.3, VT-09`) | Tech / Admin ID| Elevator ID, diagnostic reason, car state |
| **Mode 4 (Maintenance)**| `RETURN_TO_SERVICE` | Technician clears maintenance and returns car (`MNT-4.6, VT-09`)| Tech / Admin ID| Elevator ID, verification checklist confirmed |
| **Interface / Protocol**| `MALFORMED_MSG_REJECTED`| Invalid floor, unknown car, or illegal state event (`SEC-2.10, VT-28`)| System | Source interface, raw message payload, violation code |
| **Interface / Protocol**| `COMMAND_CONFLICT_REJECTED`| Conflicting command rejected (e.g. rescue to car in maintenance, `SEC-2.20, VT-32`)| Admin ID / System | Attempted command, existing car state, conflict reason |
| **Security Violations** | `UNAUTHORIZED_CMD_REJECTED`| Non-admin attempting role-restricted command (`SEC-2.9, SEC-2.13, VT-13`)| Actor ID | Attempted operation, missing required role |
| **Mode 5 (Analytics)** | `INTEGRITY_CHECK_RUN` | Admin runs cryptographic audit chain check (`ANA-5.8, VT-10`) | Admin ID | Total records inspected, result (`PASS` / `FAIL`), mismatch index |

---

## 6. Operational In-Memory State (Cache & Fast Paths)

For real-time lock-step tick performance, `ACS.AccessControl` maintains the following static in-memory state:
1. **Active Presentation Tokens / Car Sessions (`SEC-2.21`):**  
   Map: `car_id -> { session_token: str, credential_id: str, role: str, allowed_floors: list[int], granted_tick: int, expires_at_tick: int, consumed: bool }`.
   * **Role-Dependent Expiration Windows:**
     * **`RESIDENT` & `VISITOR`:** **5 seconds** (tight window to prevent tailgaters from selecting floors).
     * **`BUILDING_SERVICE` & `TECHNICIAN`:** **30 seconds** (grants staff adequate time to engage equipment or select service modes).
   * **Clearing / Invalidation:** Cleared/consumed immediately once an authorized destination or service run button is pressed, or when the role-dependent timeout expires.
   * **Override / Preemption:** If another credential is swiped in the same elevator (`same car_id`) before the active token is consumed or expired, the previous token is **immediately overridden** by the new credential presentation, granting a fresh selection window corresponding to the new role.
2. **Recent Presentations Window (`SEC-2.18`):**  
   Rolling queue of: `{ credential_id, car_id, tick_timestamp }` for the last 10 seconds to detect concurrent card cloning across cars.
3. **Failed Attempts Counter (`SEC-2.7`):**  
   Map: `credential_id -> [list of failed timestamps in last 60s]`. Alert fires when `len >= 3`.
4. **Unregistered Scan Counter (`SEC-2.19`):**  
   Sliding 5-minute set of unique unknown card IDs. Alert fires when `unique_ids.size >= 11`.

---

## 7. Amendment: Card-Based Dashboard Session Lifecycle & Sliding TTL Management (`SEC-2.13, SEC-2.15, OR-19`)

### 7.1 Motivation & Architectural Scope
While §3.1 and §6 specify short-lived in-cabin presentation tokens (`active_car_sessions`, 5s for residents, 30s for staff) for one-tap destination selection inside an elevator cab, administrative and technician interfaces (`UI-3, UI-4, UI-10`) require persistent authenticated dashboard sessions.

Per **`OR-19`** and **`SEC-2.15`**, dashboard sessions are subject to a **15-minute inactivity timeout**. To preserve authentication consistency across SREMS without introducing secondary authentication mechanisms (e.g., passwords or biometric systems, which are out of scope per SRS §2.5), **dashboard authentication remains strictly card-based**.

### 7.2 Card Swipe Payload Extension (`CardSwipePayload`)
The in-memory credential presentation contract (`CardSwipePayload`, §4.1) is augmented with an explicit destination context flag:

```python
@dataclass(frozen=True)
class CardSwipePayload:
    credential_id: str                          # Unique presented card identifier
    timestamp: datetime                         # Virtual simulation clock timestamp (T_sim)
    elevator_id: str | None = None              # 'E1'-'E5' for cab readers; None for dashboard
    is_dashboard_access: bool = False           # Flag: True for dashboard login, False for in-cabin
```

#### Entitlement & Validation Rules:
1. **Dashboard Role Entitlement (`SEC-2.13, UI-10`):**
   When `is_dashboard_access == True`, `ACS.AccessControl` verifies role permissions:
   * **`ADMIN`:** Permitted full access (Fleet monitoring, credential management, emergency overrides, analytics export, audit integrity check).
   * **`TECHNICIAN`:** Permitted diagnostic access (`UI-4, MNT-4.2, MNT-4.5`: Fleet diagnosis, maintenance mode entry/exit, return to service).
   * **`RESIDENT`, `VISITOR`, `BUILDING_SERVICE`:** Denied immediately with reason `DASHBOARD_ACCESS_DENIED`. Emits `ACCESS_DENIAL` audit record (`SEC-2.6`).
2. **Credential State Check (`SEC-2.16`):**
   The presented card must be `ACTIVE`. Any card in `SUSPENDED`, `REVOKED`, or `EXPIRED` state is rejected and logged.

### 7.3 Dashboard Session Creation & Timeout Constant
* **Virtual Inactivity Timeout Constant (`OR-19`):**
  $$\text{DASHBOARD\_SESSION\_TIMEOUT\_SECONDS} = 15 \times 60 = 900\text{ seconds (15 virtual minutes)}$$
* **Session Instantiation (`ACS.AccessControl`):**
  Upon successful validation, a session record is generated and stored in the in-memory map `active_dashboard_sessions`:
  ```python
  session_token = str(uuid4())
  expires_at = current_virtual_timestamp + timedelta(seconds=DASHBOARD_SESSION_TIMEOUT_SECONDS)

  active_dashboard_sessions[session_token] = {
      "session_token": session_token,
      "credential_id": credential_id,
      "user_id": user_id,
      "role": role,
      "created_at": current_virtual_timestamp,
      "last_active_at": current_virtual_timestamp,
      "expires_at": expires_at
  }
  ```
* **Audit Logging:** Emits an `ADMIN_LOGIN` event to `audit_log_chain` recording `session_token`, `credential_id`, and `role`.

### 7.4 Dashboard Operation Types & Role Entitlement Matrix (`SEC-2.13`)
Privileged dashboard operations are categorized via a dedicated enum mirroring the system permissions defined in §1 Table 2:

```python
class DashboardOperationType(str, Enum):
    VIEW_FLEET_STATUS        = "VIEW_FLEET_STATUS"        # Real-time positions, directions, states (UI-10)
    VIEW_DIAGNOSTICS         = "VIEW_DIAGNOSTICS"         # Fault codes, stale state, call history (UI-4, MNT-4.5)
    MAINTENANCE_CONTROL      = "MAINTENANCE_CONTROL"      # Enter maintenance mode, return to service (MNT-4.3, MNT-4.6)
    MANAGE_CREDENTIALS       = "MANAGE_CREDENTIALS"       # Issue, suspend, reinstate, revoke cards (SEC-2.16, SEC-2.17)
    EMERGENCY_OVERRIDE       = "EMERGENCY_OVERRIDE"       # Manual door override, recall, rescue car (EMR-3.8, EMR-3.12)
    VIEW_AUDIT_LOGS          = "VIEW_AUDIT_LOGS"          # Search security incidents & audit ledger (SEC-2.14, ANA-5.7)
    RUN_AUDIT_INTEGRITY      = "RUN_AUDIT_INTEGRITY"      # Cryptographic SHA-256 chain verification (ANA-5.8, VT-10)
    EXPORT_ANALYTICS_REPORTS = "EXPORT_ANALYTICS_REPORTS" # Performance KPI export CSV/JSON (ANA-5.9, BEN-4)
```

The table below defines the exact authorization mapping for dashboard operations by role:

| `DashboardOperationType` | Governing SRS Mandates | `TECHNICIAN` | `ADMIN` | Non-Dashboard Roles (`RESIDENT`, `VISITOR`, `BUILDING_SERVICE`) |
| :--- | :--- | :---: | :---: | :---: |
| `VIEW_FLEET_STATUS` | `UI-10, PERF-2` | **ALLOWED** | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `VIEW_DIAGNOSTICS` | `UI-4, MNT-4.2, MNT-4.5` | **ALLOWED** | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `MAINTENANCE_CONTROL` | `MNT-4.3, MNT-4.6, VT-09` | **ALLOWED** | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `MANAGE_CREDENTIALS` | `SEC-2.16, SEC-2.17, VT-17` | DENIED | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `EMERGENCY_OVERRIDE` | `EMR-3.8, EMR-3.12, VT-13` | DENIED | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `VIEW_AUDIT_LOGS` | `SEC-2.14, ANA-5.7, VT-29` | DENIED | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `RUN_AUDIT_INTEGRITY` | `ANA-5.8, VT-10` | DENIED | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |
| `EXPORT_ANALYTICS_REPORTS` | `ANA-5.9, BEN-4, VT-29` | DENIED | **ALLOWED** | DENIED (`DASHBOARD_ACCESS_DENIED`) |

### 7.5 `DashboardOperationPayload` & `validate_dashboard_operation` Contract
Akin to in-cabin payloads in §4, all dashboard interactions pass through a standardized validation contract:

```python
@dataclass(frozen=True)
class DashboardOperationPayload:
    session_token: str                          # Active 15-minute dashboard session UUID
    op_type: DashboardOperationType             # Targeted dashboard operation enum
    timestamp: datetime                         # Virtual simulation clock timestamp (T_sim)
    details: dict = field(default_factory=dict) # Operation arguments (e.g. car_id, target_credential_id)
```

#### Validation & Sliding TTL Logic (`validate_dashboard_operation`):
```python
def validate_dashboard_operation(payload: DashboardOperationPayload) -> AuthorizationResult:
    # 1. Session Existence Check
    session = active_dashboard_sessions.get(payload.session_token)
    if not session:
        return AuthorizationResult(
            allowed=False, 
            error_code="SESSION_INVALID", 
            message="No active dashboard session found"
        )

    # 2. Virtual Clock Inactivity Expiration Check (OR-19, SEC-2.15)
    if payload.timestamp > session["expires_at"]:
        del active_dashboard_sessions[payload.session_token]
        ACS.Logging.record_audit_event(
            action_type="ADMIN_LOGOUT",
            actor_id=session["credential_id"],
            payload={"reason": "TIMEOUT", "session_token": payload.session_token}
        )
        return AuthorizationResult(
            allowed=False, 
            error_code="SESSION_EXPIRED", 
            message="Dashboard session timed out after 15 minutes of inactivity"
        )

    # 3. Reset Sliding Inactivity Timeout (Hold & Extend TTL)
    session["expires_at"] = payload.timestamp + timedelta(seconds=DASHBOARD_SESSION_TIMEOUT_SECONDS)
    session["last_active_at"] = payload.timestamp

    # 4. Role Entitlement Verification (§7.4 Permission Matrix)
    user_role = session["role"]
    if not DASHBOARD_PERMISSIONS[user_role].get(payload.op_type, False):
        ACS.Logging.record_audit_event(
            action_type="UNAUTHORIZED_CMD_REJECTED",
            actor_id=session["credential_id"],
            payload={
                "attempted_operation": payload.op_type.value,
                "role": user_role,
                "reason": "MISSING_REQUIRED_ROLE"
            }
        )
        return AuthorizationResult(
            allowed=False, 
            error_code="UNAUTHORIZED_ROLE", 
            message=f"Role '{user_role}' is not authorized to execute '{payload.op_type.value}'"
        )

    # 5. Authorization Granted
    return AuthorizationResult(allowed=True, error_code=None, message="Authorized")
```

### 7.6 JIT Eviction vs. Key Rescanning Optimization
* **Performance Rationale:**
  Elevator simulations operate on rapid, high-frequency execution cycles (e.g., 1 Hz to 1,000 Hz in headless benchmark runs). Running a recurring background loop to scan and garbage-collect expired session keys on every tick would introduce unnecessary CPU overhead and lock contention.
* **Just-In-Time (JIT) Lazy Eviction:**
  Expired session keys are evaluated and evicted **strictly on-demand** inside `validate_dashboard_operation`. If an abandoned session is never accessed again, it remains dormant with zero compute penalty until explicitly cleared on shutdown or test reset.
* **Manual Logout:**
  An explicit dashboard logout request immediately purges `session_token` from `active_dashboard_sessions` and logs an `ADMIN_LOGOUT` event with `reason = "MANUAL"`.
