-- =============================================================================
-- SREMS Access Control System (ACS) - PostgreSQL Database & Schema Seed
-- Based on: acs/design_decisions_ACS.md
-- Execution: psql -U postgres -f acs/seed.sql
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
-- 4. Privilege Configuration for `srems_user`
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
-- 5. Initial Seed Data: 1 Admin User & 1 Admin Credential (Idempotent)
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
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'Initial System Administrator',
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
VALUES (
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
)
ON CONFLICT (credential_id) DO NOTHING;
