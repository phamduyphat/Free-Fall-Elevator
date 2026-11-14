from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import threading
from typing import Callable, Optional

import asyncpg

from acs.credential_accesscontrol import CredentialAccessControl
from db import DEFAULT_DSN

DEFAULT_BASE_TIME: datetime = datetime(2026, 1, 1, 0, 0, 0)
MORNING_PEAK_BASE_TIME: datetime = datetime(2026, 1, 1, 7, 0, 0)
EVENING_PEAK_BASE_TIME: datetime = datetime(2026, 1, 1, 17, 0, 0)
OFF_PEAK_BASE_TIME: datetime = datetime(2026, 1, 1, 22, 0, 0)

DEFAULT_SIM_SECONDS_PER_TICK: float = 1.0
DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0


def _normalize_utc_naive(ts: datetime | str) -> datetime:
    """Normalize ISO strings or tz-aware datetimes to naive UTC to match DB TIMESTAMP columns."""
    parsed = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True)
class ClockSnapshot:
    """Thread-safe snapshot of the active virtual simulation clock."""

    current_sim_time: datetime
    base_time: datetime
    tick: int
    sim_seconds_per_tick: float
    tick_interval_seconds: Optional[float]
    running: bool


class SimulationClock:
    """
    Server-side tick-based virtual clock (`T_sim = T_0 + tick * dt_sim_seconds`).

    Runs a sequential tick-then-sleep daemon thread with a dedicated asyncio event
    loop and a single persistent PostgreSQL connection so heavy or synchronous
    simulation steps never stall FastAPI's event loop and sleep-until-next-tick
    only begins after the current tick finishes.
    """

    _lock: threading.Lock = threading.Lock()
    _base_time: datetime = DEFAULT_BASE_TIME
    _tick: int = 0
    _sim_seconds_per_tick: float = DEFAULT_SIM_SECONDS_PER_TICK
    _tick_interval_seconds: Optional[float] = DEFAULT_TICK_INTERVAL_SECONDS
    current_time: datetime = DEFAULT_BASE_TIME

    _thread: Optional[threading.Thread] = None
    _stop_event: Optional[threading.Event] = None
    _tick_hook: Optional[Callable[[datetime, int], None]] = None
    _loop_db_conn: Optional[asyncpg.Connection] = None

    @classmethod
    def now(cls) -> datetime:
        """Return the current virtual simulation timestamp (naive UTC)."""
        with cls._lock:
            return cls.current_time

    @classmethod
    def snapshot(cls) -> ClockSnapshot:
        """Return an atomic snapshot of all virtual clock parameters and state."""
        with cls._lock:
            running = cls._thread is not None and cls._thread.is_alive()
            return ClockSnapshot(
                current_sim_time=cls.current_time,
                base_time=cls._base_time,
                tick=cls._tick,
                sim_seconds_per_tick=cls._sim_seconds_per_tick,
                tick_interval_seconds=cls._tick_interval_seconds,
                running=running,
            )

    @classmethod
    def set_tick_hook(
        cls, hook: Optional[Callable[[datetime, int], None]]
    ) -> None:
        """Register an optional synchronous callback invoked on each tick (None for no-op)."""
        with cls._lock:
            cls._tick_hook = hook

    @classmethod
    async def _open_persistent_connection(cls) -> Optional[asyncpg.Connection]:
        """Open a single persistent asyncpg connection for the current clock loop."""
        dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
        try:
            return await asyncpg.connect(dsn=dsn, timeout=1.0)
        except (OSError, asyncpg.PostgresError, TimeoutError):
            return None

    @classmethod
    async def _execute_single_tick_async(
        cls, db_conn: Optional[asyncpg.Connection] = None
    ) -> datetime:
        """Run bulk credential transitions, then tick hooks, then advance virtual clock state."""
        with cls._lock:
            hook = cls._tick_hook
            next_tick = cls._tick + 1
            next_time = cls._base_time + timedelta(
                seconds=next_tick * cls._sim_seconds_per_tick
            )

        if db_conn is None or db_conn.is_closed():
            db_conn = await cls._open_persistent_connection()
            cls._loop_db_conn = db_conn

        if db_conn is not None and not db_conn.is_closed():
            await CredentialAccessControl.apply_bulk_transitions(
                next_time,
                conn=db_conn,
            )

        # NOTE: Subsequent actions in this tick MUST come after the bulk card state transition above
        # so that expired visitor passes and auto-revoked lost cards are reflected before any tick actions run.
        if hook is not None:
            hook(next_time, next_tick)

        with cls._lock:
            cls._tick = next_tick
            cls.current_time = cls._base_time + timedelta(
                seconds=cls._tick * cls._sim_seconds_per_tick
            )
            return cls.current_time

    @classmethod
    def _cleanup_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Close any active persistent loop connection and tear down the thread's event loop."""
        active_conn = cls._loop_db_conn
        cls._loop_db_conn = None
        if active_conn is not None and not active_conn.is_closed():
            try:
                loop.run_until_complete(active_conn.close())
            except Exception:
                pass
        asyncio.set_event_loop(None)
        loop.close()

    @classmethod
    def _run_loop(
        cls, stop_event: threading.Event, interval_seconds: float
    ) -> None:
        """Sequential loop: hold one persistent DB connection and wait only after each tick completes."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        cls._loop_db_conn = None
        try:
            while not stop_event.wait(timeout=interval_seconds):
                if stop_event.is_set():
                    break

                # Run a single tick
                loop.run_until_complete(cls._execute_single_tick_async(cls._loop_db_conn))
        finally:
            cls._cleanup_loop(loop)

    @classmethod
    def _run_advance_steps(cls, steps: int) -> None:
        """Execute `steps` sequential ticks in CPU clock time using one persistent DB connection."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        cls._loop_db_conn = None
        try:
            async def _run_batch() -> None:
                for _ in range(steps):
                    await cls._execute_single_tick_async(cls._loop_db_conn)

            loop.run_until_complete(_run_batch())
        finally:
            cls._cleanup_loop(loop)

    @classmethod
    def stop(cls, timeout: float = 2.0) -> None:
        """Signal the background ticker thread to stop and wait for clean exit."""
        with cls._lock:
            stop_event = cls._stop_event
            thread = cls._thread
            cls._stop_event = None
            cls._thread = None

        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    @classmethod
    def start(cls) -> bool:
        """Start the background daemon ticker thread if an interval > 0 is configured."""
        with cls._lock:
            interval = cls._tick_interval_seconds
            if interval is None or interval <= 0:
                return False
            if cls._thread is not None and cls._thread.is_alive():
                return True
            stop_event = threading.Event()
            thread = threading.Thread(
                target=cls._run_loop,
                args=(stop_event, interval),
                name="srems-sim-clock",
                daemon=True,
            )
            cls._stop_event = stop_event
            cls._thread = thread
            thread.start()
            return True

    @classmethod
    def reset(
        cls,
        *,
        base_time: datetime | str = DEFAULT_BASE_TIME,
        sim_seconds_per_tick: float = DEFAULT_SIM_SECONDS_PER_TICK,
        tick_interval_seconds: Optional[float] = DEFAULT_TICK_INTERVAL_SECONDS,
        start_loop: bool = True,
    ) -> ClockSnapshot:
        """Stop any active clock thread, reset counters to tick=0, and optionally start auto-ticking."""
        if sim_seconds_per_tick <= 0:
            raise ValueError("sim_seconds_per_tick must be greater than 0.")
        if tick_interval_seconds is not None and tick_interval_seconds < 0:
            raise ValueError("tick_interval_seconds cannot be negative.")

        cls.stop()
        normalized_base = _normalize_utc_naive(base_time)

        with cls._lock:
            cls._base_time = normalized_base
            cls._tick = 0
            cls._sim_seconds_per_tick = float(sim_seconds_per_tick)
            cls._tick_interval_seconds = (
                float(tick_interval_seconds)
                if tick_interval_seconds is not None
                else None
            )
            cls.current_time = normalized_base

        if start_loop:
            cls.start()
        return cls.snapshot()

    @classmethod
    def advance(cls, steps: int = 1) -> ClockSnapshot:
        """Run the tick loop `steps` times sequentially in CPU clock time."""
        if steps < 0:
            raise ValueError("steps must be non-negative.")
        if steps == 0:
            return cls.snapshot()

        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if in_running_loop:
            error: list[BaseException] = []

            def _worker() -> None:
                try:
                    cls._run_advance_steps(steps)
                except BaseException as exc:
                    error.append(exc)

            worker = threading.Thread(
                target=_worker, name="srems-sim-advance", daemon=True
            )
            worker.start()
            worker.join()
            if error:
                raise error[0]
        else:
            cls._run_advance_steps(steps)

        return cls.snapshot()
