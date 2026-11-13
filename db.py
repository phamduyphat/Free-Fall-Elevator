from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from typing import Any, Optional

import asyncpg

DEFAULT_DSN = "postgresql://srems_user:srems_password@localhost:5432/srems"


class DbConnectionPool:
    """Static/singleton PostgreSQL connection pool manager powered by asyncpg."""

    _pool: Optional[asyncpg.Pool] = None

    @classmethod
    async def init_pool(
        cls,
        dsn: Optional[str] = None,
        *,
        min_size: int = 2,
        max_size: int = 10,
        **kwargs: Any,
    ) -> asyncpg.Pool:
        """Initialize the shared asyncpg connection pool if not already open."""
        if cls._pool is None:
            resolved_dsn = dsn or os.getenv("DATABASE_URL", DEFAULT_DSN)
            cls._pool = await asyncpg.create_pool(
                dsn=resolved_dsn,
                min_size=min_size,
                max_size=max_size,
                **kwargs,
            )
        return cls._pool

    @classmethod
    async def close_pool(cls) -> None:
        """Close the shared asyncpg connection pool if open."""
        if cls._pool is not None:
            await cls._pool.close()
            cls._pool = None

    @classmethod
    def get_pool(cls) -> asyncpg.Pool:
        """Return the active connection pool or raise if uninitialized."""
        if cls._pool is None:
            raise RuntimeError(
                "Database connection pool is not initialized. "
                "Call `await DbConnectionPool.init_pool()` during application startup."
            )
        return cls._pool

    @classmethod
    @asynccontextmanager
    async def acquire(cls) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a single PostgreSQL connection from the pool."""
        pool = cls.get_pool()
        async with pool.acquire() as connection:
            yield connection

    @classmethod
    @asynccontextmanager
    async def transaction(cls) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection and execute within an atomic ACID transaction."""
        async with cls.acquire() as connection:
            async with connection.transaction():
                yield connection

    @classmethod
    async def reset_database_tables(cls) -> None:
        """
        Delete all rows from all SREMS tables and re-seed the default Admin user
        and Admin credential (matching `seed.sql`) without dropping tables or DB.
        """
        async with cls.transaction() as conn:
            # Delete in child-to-parent order to respect foreign key constraints
            await conn.execute("DELETE FROM incidents WHERE TRUE;")
            await conn.execute("DELETE FROM car_downtime_intervals WHERE TRUE;")
            await conn.execute("DELETE FROM trips WHERE TRUE;")
            await conn.execute("DELETE FROM car_calls WHERE TRUE;")
            await conn.execute("DELETE FROM hall_calls WHERE TRUE;")
            await conn.execute("DELETE FROM system_heartbeats WHERE TRUE;")
            await conn.execute("DELETE FROM audit_log_chain WHERE TRUE;")
            await conn.execute("DELETE FROM access_authorization_events WHERE TRUE;")
            await conn.execute("DELETE FROM security_alerts WHERE TRUE;")
            await conn.execute("DELETE FROM credentials WHERE TRUE;")
            await conn.execute("DELETE FROM users WHERE TRUE;")

            # Reset the identity sequences back to 1
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('audit_log_chain', 'log_index'), 1, false);"
            )
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('system_heartbeats', 'heartbeat_id'), 1, false);"
            )

            # Re-seed initial Admin user and Admin card matching seed.sql
            await conn.execute(
                """
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
                """
            )

            await conn.execute(
                """
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
                """
            )


async def get_db_connection() -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency that yields an acquired PostgreSQL connection from the pool."""
    async with DbConnectionPool.acquire() as connection:
        yield connection


def get_db_pool() -> type[DbConnectionPool]:
    """FastAPI dependency that provides the initialized DbConnectionPool class."""
    DbConnectionPool.get_pool()
    return DbConnectionPool
