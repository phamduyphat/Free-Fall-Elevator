from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Callable, Optional

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

    Runs a sequential tick-then-sleep daemon thread so heavy or synchronous
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
    def _execute_single_tick(cls) -> datetime:
        """Execute the tick action hook (if any) and advance virtual clock state sequentially."""
        with cls._lock:
            hook = cls._tick_hook
            next_tick = cls._tick + 1
            next_time = cls._base_time + timedelta(
                seconds=next_tick * cls._sim_seconds_per_tick
            )

        if hook is not None:
            hook(next_time, next_tick)

        with cls._lock:
            cls._tick = next_tick
            cls.current_time = cls._base_time + timedelta(
                seconds=cls._tick * cls._sim_seconds_per_tick
            )
            return cls.current_time

    @classmethod
    def _run_loop(
        cls, stop_event: threading.Event, interval_seconds: float
    ) -> None:
        """Sequential loop: wait for interval only after each tick completes."""
        while not stop_event.wait(timeout=interval_seconds):
            if stop_event.is_set():
                break
            cls._execute_single_tick()

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
        """Synchronously advance the virtual clock by `steps` sequential ticks."""
        if steps < 0:
            raise ValueError("steps must be non-negative.")
        for _ in range(steps):
            cls._execute_single_tick()
        return cls.snapshot()

