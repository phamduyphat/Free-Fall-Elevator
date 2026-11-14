-- =============================================================================
-- SREMS Database & Schema Seed (ACS & Analytics / Operations)
-- Based on: acs/design_decisions_ACS.md & analytics/design_decisions_analytics.md
-- Execution: psql -U postgres -f seed.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Create Application Role (CRUD only; cannot DROP tables or DROP database)
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'srems_user') THEN
        CREATE ROLE srems_user WITH
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOREPLICATION
            PASSWORD 'srems_password';
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 2. Create Database 'srems' If It Does Not Exist
-- -----------------------------------------------------------------------------
SELECT 'CREATE DATABASE srems OWNER postgres ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'srems')\gexec

-- Connect to the 'srems' database (psql meta-command)
\connect srems

-- Enable pgcrypto for UUID and SHA-256 helpers
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- -----------------------------------------------------------------------------
-- 3. Table Definitions (Section 5 of design_decisions_ACS.md)
-- -----------------------------------------------------------------------------

-- 5.1 `users` (Cardholders & Operators)
CREATE TABLE IF NOT EXISTS users (
    user_id         VARCHAR(36)  PRIMARY KEY DEFAULT gen_random_uuid()::text,
    full_name       VARCHAR(100) NOT NULL,
    role            VARCHAR(30)  NOT NULL CHECK (
        role IN (
            'RESIDENT',
            'VISITOR',
            'BUILDING_SERVICE',
            'TECHNICIAN',
            'ADMIN'
        )
    ),
    apartment_unit  VARCHAR(10)  NULL,
    assigned_floor  INT          NULL CHECK (assigned_floor IS NULL OR assigned_floor = -1 OR (assigned_floor BETWEEN 1 AND 30)),
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 5.2 `credentials` (Cards & Passes)
CREATE TABLE IF NOT EXISTS credentials (
    credential_id     VARCHAR(64) PRIMARY KEY,
    user_id           VARCHAR(36) NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    credential_type   VARCHAR(20) NOT NULL CHECK (
        credential_type IN (
            'RESIDENT',
            'VISITOR',
            'STAFF',
            'TECHNICIAN',
            'ADMIN'
        )
    ),
    status            VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (
        status IN (
            'ACTIVE',
            'SUSPENDED',
            'REVOKED',
            'EXPIRED'
        )
    ),
    suspension_reason VARCHAR(30) NULL CHECK (
        suspension_reason IS NULL OR suspension_reason IN (
            'LOST',
            'CLONE_SUSPECTED',
            'ADMIN_MANUAL'
        )
    ),
    suspension_start  TIMESTAMP   NULL,
    revocation_reason VARCHAR(50) NULL,
    revoked_at        TIMESTAMP   NULL,
    expires_at        TIMESTAMP   NULL,
    allowed_floors    JSONB       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allowed_floors) = 'array'),
    issued_by_admin   VARCHAR(36) NULL REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE,
    created_at        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_credentials_user_id ON credentials (user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials (status);

-- 5.3 `access_authorization_events` (Operational Decisions & Denials)
-- Note: `credential_id` is intentionally not a strict FK to `credentials` so that
-- unregistered card presentations (`SEC-2.19`) can still be recorded and audited.
CREATE TABLE IF NOT EXISTS access_authorization_events (
    event_id          VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    timestamp         TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    credential_id     VARCHAR(64) NULL,
    elevator_id       VARCHAR(5)  NOT NULL CHECK (elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    origin_floor      INT         NOT NULL CHECK (origin_floor = -1 OR (origin_floor BETWEEN 1 AND 30)),
    destination_floor INT         NOT NULL CHECK (destination_floor = -1 OR (destination_floor BETWEEN 1 AND 30)),
    outcome           VARCHAR(10) NOT NULL CHECK (outcome IN ('ALLOWED', 'DENIED')),
    denial_reason     VARCHAR(40) NULL CHECK (
        denial_reason IS NULL OR denial_reason IN (
            'UNREGISTERED_CARD',
            'CARD_SUSPENDED',
            'CARD_REVOKED',
            'CARD_EXPIRED',
            'UNAUTHORIZED_FLOOR',
            'E5_B1_FORBIDDEN',
            'ONE_TAP_LIMIT_EXCEEDED',
            'TOKEN_WINDOW_EXPIRED',
            'NO_ACTIVE_CREDENTIAL',
            'DB_FAIL_CLOSED'
        )
    ),
    alert_triggered   BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_auth_events_timestamp ON access_authorization_events (timestamp);
CREATE INDEX IF NOT EXISTS idx_auth_events_credential_id ON access_authorization_events (credential_id);

-- 5.4 `security_alerts` (Security Incident Tracking - SEC-2.8)
CREATE TABLE IF NOT EXISTS security_alerts (
    alert_id         VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    severity         VARCHAR(15) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    alert_type       VARCHAR(40) NOT NULL CHECK (
        alert_type IN (
            'REPEATED_FAILURES',
            'CLONED_CREDENTIAL',
            'CARD_SCANNING',
            'LOST_CARD_USAGE',
            'REVOKED_CARD_USAGE',
            'DOOR_OBSTRUCTION'
        )
    ),
    credential_id    VARCHAR(64) NULL,
    primary_car_id   VARCHAR(5)  NULL CHECK (primary_car_id IS NULL OR primary_car_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    secondary_car_id VARCHAR(5)  NULL CHECK (secondary_car_id IS NULL OR secondary_car_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    details          TEXT        NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    resolved_by      VARCHAR(36) NULL REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE,
    created_at       TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at      TIMESTAMP   NULL
);

CREATE INDEX IF NOT EXISTS idx_security_alerts_status ON security_alerts (status);
CREATE INDEX IF NOT EXISTS idx_security_alerts_created_at ON security_alerts (created_at);

-- 5.5 `audit_log_chain` (Immutable Tamper-Evident Ledger - SEC-2.11, SEC-2.12, ANA-5.8)
CREATE TABLE IF NOT EXISTS audit_log_chain (
    log_index     BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    timestamp     TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_id      VARCHAR(64) NOT NULL,
    actor_role    VARCHAR(30) NOT NULL,
    action_type   VARCHAR(50) NOT NULL,
    target_id     VARCHAR(64) NOT NULL,
    event_payload JSONB       NOT NULL,
    prev_hash     CHAR(64)    NOT NULL,
    current_hash  CHAR(64)    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_chain_timestamp ON audit_log_chain (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_chain_action_type ON audit_log_chain (action_type);

-- -----------------------------------------------------------------------------
-- 4. Mode 5 (Analytics) & Operational Tables (Section 5 of design_decisions_analytics.md)
-- -----------------------------------------------------------------------------

-- 4.1 `trips` (Cab Physical Movement Legs - DAT-1, ANA-5.1, ANA-5.4)
CREATE TABLE IF NOT EXISTS trips (
    trip_id             VARCHAR(36)      PRIMARY KEY DEFAULT gen_random_uuid()::text,
    elevator_id         VARCHAR(5)       NOT NULL CHECK (elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    origin_floor        INT              NOT NULL CHECK (origin_floor = -1 OR (origin_floor BETWEEN 1 AND 30)),
    destination_floor   INT              NOT NULL CHECK (destination_floor = -1 OR (destination_floor BETWEEN 1 AND 30)),
    departure_time      TIMESTAMP        NOT NULL,
    arrival_time        TIMESTAMP        NOT NULL,
    travel_time_seconds DOUBLE PRECISION NOT NULL,
    floors_traversed    INT              NOT NULL,
    motor_starts        INT              NOT NULL DEFAULT 1,
    traffic_profile     VARCHAR(20)      NOT NULL CHECK (traffic_profile IN ('PEAK', 'OFF_PEAK', 'NORMAL', 'STRESS')),
    is_service_run      BOOLEAN          NOT NULL DEFAULT FALSE,
    is_estimated        BOOLEAN          NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_trips_elevator_id ON trips (elevator_id);
CREATE INDEX IF NOT EXISTS idx_trips_departure_time ON trips (departure_time);
CREATE INDEX IF NOT EXISTS idx_trips_arrival_time ON trips (arrival_time);
CREATE INDEX IF NOT EXISTS idx_trips_traffic_profile ON trips (traffic_profile);

-- 4.2 `hall_calls` (Hall Request Lifecycle - DAT-1, ANA-5.1, FR-1.7, FR-1.18, FR-1.21)
CREATE TABLE IF NOT EXISTS hall_calls (
    call_id              VARCHAR(36)      PRIMARY KEY DEFAULT gen_random_uuid()::text,
    origin_floor         INT              NOT NULL CHECK (origin_floor = -1 OR (origin_floor BETWEEN 1 AND 30)),
    direction            VARCHAR(5)       NOT NULL CHECK (direction IN ('UP', 'DOWN')),
    is_freight           BOOLEAN          NOT NULL DEFAULT FALSE,
    assigned_elevator_id VARCHAR(5)       NULL CHECK (assigned_elevator_id IS NULL OR assigned_elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    status               VARCHAR(30)      NOT NULL CHECK (
        status IN (
            'RECEIVED',
            'QUEUED',
            'ASSIGNED',
            'APPROACHING',
            'ARRIVED',
            'COMPLETED',
            'CANCELLED',
            'FAILED',
            'BLOCKED'
        )
    ),
    created_at           TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    assigned_at          TIMESTAMP        NULL,
    approaching_at       TIMESTAMP        NULL,
    arrived_at           TIMESTAMP        NULL,
    completed_at         TIMESTAMP        NULL,
    wait_time_seconds    DOUBLE PRECISION NULL,
    reassignments_count  INT              NOT NULL DEFAULT 0,
    no_boarding          BOOLEAN          NOT NULL DEFAULT FALSE,
    cancellation_reason  VARCHAR(50)      NULL,
    traffic_profile      VARCHAR(20)      NULL CHECK (
        traffic_profile IS NULL OR traffic_profile IN ('PEAK', 'OFF_PEAK', 'NORMAL', 'STRESS')
    )
);

CREATE INDEX IF NOT EXISTS idx_hall_calls_status ON hall_calls (status);
CREATE INDEX IF NOT EXISTS idx_hall_calls_created_at ON hall_calls (created_at);
CREATE INDEX IF NOT EXISTS idx_hall_calls_assigned_elevator ON hall_calls (assigned_elevator_id);
CREATE INDEX IF NOT EXISTS idx_hall_calls_origin_floor ON hall_calls (origin_floor);
CREATE INDEX IF NOT EXISTS idx_hall_calls_traffic_profile ON hall_calls (traffic_profile);

-- 4.3 `car_calls` (In-Cabin Destination Request Lifecycle - DAT-1, ANA-5.1, FR-1.8, FR-1.9, FR-1.12)
CREATE TABLE IF NOT EXISTS car_calls (
    call_id            VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    elevator_id        VARCHAR(5)  NOT NULL CHECK (elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    destination_floor  INT         NOT NULL CHECK (destination_floor = -1 OR (destination_floor BETWEEN 1 AND 30)),
    credential_id      VARCHAR(64) NULL,
    status             VARCHAR(30) NOT NULL CHECK (
        status IN (
            'RECEIVED',
            'AUTHORIZATION_CHECK',
            'QUEUED',
            'IN_TRANSIT',
            'COMPLETED',
            'REJECTED',
            'CANCELLED',
            'BLOCKED'
        )
    ),
    created_at         TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    authorized_at      TIMESTAMP   NULL,
    command_sent_at    TIMESTAMP   NULL,
    completed_at       TIMESTAMP   NULL,
    rejection_reason   VARCHAR(40) NULL
);

CREATE INDEX IF NOT EXISTS idx_car_calls_elevator_id ON car_calls (elevator_id);
CREATE INDEX IF NOT EXISTS idx_car_calls_status ON car_calls (status);
CREATE INDEX IF NOT EXISTS idx_car_calls_created_at ON car_calls (created_at);

-- 4.4 `car_downtime_intervals` (Availability & Maintenance - DAT-1, ANA-5.5, MNT-4.1..4.7)
CREATE TABLE IF NOT EXISTS car_downtime_intervals (
    interval_id      VARCHAR(36)      PRIMARY KEY DEFAULT gen_random_uuid()::text,
    elevator_id      VARCHAR(5)       NOT NULL CHECK (elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    state            VARCHAR(20)      NOT NULL CHECK (state IN ('MAINTENANCE', 'FAULT', 'EMERGENCY')),
    reason           TEXT             NOT NULL,
    started_at       TIMESTAMP        NOT NULL,
    ended_at         TIMESTAMP        NULL,
    duration_seconds DOUBLE PRECISION NULL,
    technician_id    VARCHAR(36)      NULL REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_car_downtime_elevator_id ON car_downtime_intervals (elevator_id);
CREATE INDEX IF NOT EXISTS idx_car_downtime_state ON car_downtime_intervals (state);
CREATE INDEX IF NOT EXISTS idx_car_downtime_started_at ON car_downtime_intervals (started_at);
CREATE INDEX IF NOT EXISTS idx_car_downtime_technician ON car_downtime_intervals (technician_id);

-- 4.5 `system_heartbeats` (SREMS Availability Ledger - AVL-1)
CREATE TABLE IF NOT EXISTS system_heartbeats (
    heartbeat_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    timestamp             TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status                VARCHAR(10) NOT NULL CHECK (status IN ('HEALTHY', 'DEGRADED', 'RECOVERING')),
    active_cars_count     INT         NOT NULL CHECK (active_cars_count BETWEEN 0 AND 5),
    active_requests_count INT         NOT NULL CHECK (active_requests_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_system_heartbeats_timestamp ON system_heartbeats (timestamp);
CREATE INDEX IF NOT EXISTS idx_system_heartbeats_status ON system_heartbeats (status);

-- 4.6 `incidents` (Emergency Incident Tracking - DAT-1, EMR-3.4, ANA-5.7)
CREATE TABLE IF NOT EXISTS incidents (
    incident_id    VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    emergency_type VARCHAR(30) NOT NULL CHECK (
        emergency_type IN (
            'FIRE_ALARM',
            'POWER_FAILURE',
            'MEDICAL_EMERGENCY',
            'STUCK_CAR',
            'DOOR_OBSTRUCTION',
            'COMMUNICATION_LOSS',
            'OVERLOAD'
        )
    ),
    scope          VARCHAR(20) NOT NULL CHECK (scope IN ('BUILDING_WIDE', 'CAR_SPECIFIC')),
    elevator_id    VARCHAR(5)  NULL CHECK (elevator_id IS NULL OR elevator_id IN ('E1', 'E2', 'E3', 'E4', 'E5')),
    floor          INT         NULL CHECK (floor IS NULL OR floor = -1 OR (floor BETWEEN 1 AND 30)),
    trigger_source VARCHAR(30) NOT NULL CHECK (
        trigger_source IN (
            'PASSENGER_HELP',
            'PASSENGER_MEDICAL',
            'SENSOR_TRIP',
            'ADMIN_MANUAL',
            'TIMEOUT'
        )
    ),
    status         VARCHAR(20) NOT NULL DEFAULT 'OPEN' CHECK (
        status IN (
            'OPEN',
            'RESCUE_DISPATCHED',
            'RESOLVED',
            'FALSE_ALARM'
        )
    ),
    created_at     TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at    TIMESTAMP   NULL,
    resolved_by    VARCHAR(36) NULL REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON incidents (created_at);
CREATE INDEX IF NOT EXISTS idx_incidents_elevator_id ON incidents (elevator_id);
CREATE INDEX IF NOT EXISTS idx_incidents_emergency_type ON incidents (emergency_type);

-- -----------------------------------------------------------------------------
-- 5. Privilege Configuration for `srems_user`
--    Grants CRUD (SELECT, INSERT, UPDATE, DELETE) on all tables in `srems`,
--    while preventing DROP DATABASE, DROP TABLE, ALTER TABLE, or CREATE TABLE.
-- -----------------------------------------------------------------------------
REVOKE ALL ON DATABASE srems FROM PUBLIC;
GRANT CONNECT ON DATABASE srems TO srems_user;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM srems_user;
GRANT USAGE ON SCHEMA public TO srems_user;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO srems_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO srems_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO srems_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO srems_user;

-- -----------------------------------------------------------------------------
-- 6. Initial Seed Data: 2 Admin Users & 2 Admin Credentials (Idempotent)
-- -----------------------------------------------------------------------------
INSERT INTO users (
    user_id,
    full_name,
    role,
    apartment_unit,
    assigned_floor,
    created_at,
    updated_at
)
VALUES
    (
        '00000000-0000-0000-0000-000000000001',
        'Initial System Administrator',
        'ADMIN',
        NULL,
        NULL,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        '00000000-0000-0000-0000-000000000002',
        'Secondary System Administrator',
        'ADMIN',
        NULL,
        NULL,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO credentials (
    credential_id,
    user_id,
    credential_type,
    status,
    suspension_reason,
    suspension_start,
    expires_at,
    allowed_floors,
    issued_by_admin,
    created_at,
    updated_at
)
VALUES
    (
        'ADMIN-CARD-0001',
        '00000000-0000-0000-0000-000000000001',
        'ADMIN',
        'ACTIVE',
        NULL,
        NULL,
        NULL,
        '[-1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]'::jsonb,
        '00000000-0000-0000-0000-000000000001',
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        'ADMIN-CARD-0002',
        '00000000-0000-0000-0000-000000000002',
        'ADMIN',
        'ACTIVE',
        NULL,
        NULL,
        NULL,
        '[-1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]'::jsonb,
        '00000000-0000-0000-0000-000000000001',
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )
ON CONFLICT (credential_id) DO NOTHING;
