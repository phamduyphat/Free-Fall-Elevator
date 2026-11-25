"""Wear-and-tear accounting and usage balancing for the elevator fleet (NFR WEAR-1 .. WEAR-6).

Instructor requirement (project phase 2, 17 Nov 2026)
----------------------------------------------------
"The elevator controller algorithm shall balance the wear and tear of each unit as evenly
as practical. The difference between the distances (number of floors) accumulated by all
units shall be kept under 2 % a month (between the minimum and maximum distances) and at
most 5 % in 6 months. The trip counter is reset every 6 months."

How this module meets it
------------------------
* Every floor a car moves is counted on a per-car *trip counter* (odometer), together with
  motor starts and door cycles, which are the other two usage drivers of a traction lift.
* Counters exist for three windows: the current calendar month, the current 6-month period
  and the lifetime of the car. Month counters roll over on the first day of every month
  (UTC); period counters are reset every ``period_months`` months (default 6).
* ``spread_pct = (max - min) / max * 100`` is computed per dispatch group. The residential
  group is E1-E4. E5 is the only car that may serve freight calls and the only car that may
  not serve ordinary hall calls (FR-1.4, FR-1.13), so it forms a group of its own and is
  reported separately; balancing it against E1-E4 would require breaking FR-1.13.
* Dispatch adds a bounded *usage penalty* to the predicted service time of each eligible
  car (see ``usage_penalties``). The penalty grows with how far a car is ahead of the
  least-used eligible car in the same group, relative to the monthly limit, and is capped
  so that a passenger never waits more than ``max_penalty_seconds`` extra because of it.
* The penalty is paused while the group is busy (more active hall calls than eligible cars,
  ``OperationalSimulator.wear_busy_calls_per_car``): in a rush every car is needed, and steering
  calls to the least-used car only fills it up. Quiet periods are enough to keep the balance.

The module is pure Python and has no database dependency, so it can be unit-tested and used
by the offline benchmark (``simulation/wear_benchmark.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import os
from typing import Iterable

RESIDENTIAL_GROUP = "RESIDENTIAL"
FREIGHT_GROUP = "FREIGHT"
DEFAULT_GROUPS: dict[str, tuple[str, ...]] = {
    RESIDENTIAL_GROUP: ("E1", "E2", "E3", "E4"),
    FREIGHT_GROUP: ("E5",),
}


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def month_floor(ts: datetime) -> datetime:
    """First instant of the calendar month (UTC) that contains ``ts``."""
    ts = _utc(ts)
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(ts: datetime, months: int) -> datetime:
    """Add whole calendar months to a month-start timestamp."""
    ts = _utc(ts)
    index = ts.year * 12 + (ts.month - 1) + months
    return ts.replace(year=index // 12, month=index % 12 + 1, day=1)


def spread_pct(values: Iterable[float]) -> float:
    """Relative spread between the least- and most-used unit: (max - min) / max * 100.

    Returns 0.0 when nothing has been travelled yet (no imbalance to report).
    """
    vals = [float(v) for v in values]
    if not vals:
        return 0.0
    high = max(vals)
    if high <= 0:
        return 0.0
    return (high - min(vals)) / high * 100.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class WearConfig:
    """Configurable thresholds (SRS 3.5: thresholds shall be configuration data)."""

    monthly_limit_pct: float = 2.0
    period_limit_pct: float = 5.0
    period_months: int = 6
    balancing_enabled: bool = True
    # Penalty (seconds) applied to a car that is a full monthly limit ahead of the
    # least-used eligible car of its group. Proportional below that.
    gain_seconds: float = 60.0
    # Hard cap on the penalty, so the balancing never costs a passenger more than this.
    # 15 s chosen with simulation.dispatch_benchmark (55 scenarios) and a 6-month wear run:
    # same wear compliance as 30 s, lower waits.
    max_penalty_seconds: float = 15.0
    # Early in a month every counter is small; this floor keeps the relative error
    # from being dominated by single trips (floors).
    reference_floors: float = 3000.0
    groups: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_GROUPS))

    @classmethod
    def from_env(cls) -> "WearConfig":
        enabled = os.getenv("SREMS_WEAR_BALANCING", "1").strip().lower() not in ("0", "false", "no", "off")
        months = int(_env_float("SREMS_WEAR_PERIOD_MONTHS", 6))
        return cls(
            monthly_limit_pct=_env_float("SREMS_WEAR_MONTHLY_LIMIT_PCT", 2.0),
            period_limit_pct=_env_float("SREMS_WEAR_PERIOD_LIMIT_PCT", 5.0),
            period_months=max(1, months),
            balancing_enabled=enabled,
            gain_seconds=_env_float("SREMS_WEAR_GAIN_SECONDS", 60.0),
            max_penalty_seconds=_env_float("SREMS_WEAR_MAX_PENALTY_SECONDS", 15.0),
            reference_floors=max(1.0, _env_float("SREMS_WEAR_REFERENCE_FLOORS", 3000.0)),
        )


@dataclass
class CarWear:
    """Usage counters of one car. ``*_month`` and ``*_period`` are the resettable trip counters."""

    elevator_id: str
    group: str
    floors_month: int = 0
    floors_period: int = 0
    floors_lifetime: int = 0
    starts_month: int = 0
    starts_period: int = 0
    starts_lifetime: int = 0
    doors_month: int = 0
    doors_period: int = 0
    doors_lifetime: int = 0


class WearTracker:
    """Per-car trip counters with calendar-month and 6-month windows."""

    def __init__(
        self,
        elevator_ids: Iterable[str],
        start: datetime,
        config: WearConfig | None = None,
        *,
        period_start: datetime | None = None,
    ) -> None:
        self.config = config or WearConfig.from_env()
        self._group_of: dict[str, str] = {}
        for group, members in self.config.groups.items():
            for eid in members:
                self._group_of[eid] = group
        self.cars: dict[str, CarWear] = {
            eid: CarWear(eid, self._group_of.get(eid, eid)) for eid in elevator_ids
        }
        self.month_start = month_floor(start)
        self.period_start = month_floor(period_start) if period_start else self.month_start
        # The period may have started before the current month (e.g. restored state).
        while add_months(self.period_start, self.config.period_months) <= self.month_start:
            self.period_start = add_months(self.period_start, self.config.period_months)
        self.month_history: list[dict] = []
        self.period_history: list[dict] = []
        # Rows closed since the last database sync (drained by the simulator).
        self.unsynced_history: list[dict] = []

    # ------------------------------------------------------------------ windows
    @property
    def month_end(self) -> datetime:
        return add_months(self.month_start, 1)

    @property
    def period_end(self) -> datetime:
        return add_months(self.period_start, self.config.period_months)

    def next_boundary(self) -> datetime:
        """The next instant at which a counter window closes (month end, sometimes period end)."""
        return min(self.month_end, self.period_end)

    def group_of(self, elevator_id: str) -> str:
        return self._group_of.get(elevator_id, elevator_id)

    def group_members(self, group: str) -> list[str]:
        return [eid for eid in self.cars if self.group_of(eid) == group]

    # ---------------------------------------------------------------- recording
    def record_move(self, elevator_id: str, floors: int = 1) -> None:
        car = self.cars[elevator_id]
        car.floors_month += floors
        car.floors_period += floors
        car.floors_lifetime += floors

    def record_start(self, elevator_id: str) -> None:
        car = self.cars[elevator_id]
        car.starts_month += 1
        car.starts_period += 1
        car.starts_lifetime += 1

    def record_door_cycle(self, elevator_id: str) -> None:
        car = self.cars[elevator_id]
        car.doors_month += 1
        car.doors_period += 1
        car.doors_lifetime += 1

    # ------------------------------------------------------------- rollover
    def _window_rows(self, window_type: str, start: datetime, end: datetime, attr: str,
                     limit_pct: float, closed_at: datetime) -> list[dict]:
        rows = []
        for group in self.config.groups:
            members = self.group_members(group)
            if not members:
                continue
            per_car = {eid: int(getattr(self.cars[eid], attr)) for eid in members}
            spread = spread_pct(per_car.values())
            if len(members) < 2:
                within = True  # a single-car group has nothing to balance against
            elif window_type == "MONTH":
                within = spread < limit_pct  # "kept under 2 % a month"
            else:
                within = spread <= limit_pct  # "at most 5 % in 6 months"
            rows.append({
                "window_id": f"{window_type}:{start.strftime('%Y-%m')}:{group}",
                "window_type": window_type,
                "wear_group": group,
                "window_start": start,
                "window_end": end,
                "per_car_floors": per_car,
                "spread_pct": round(spread, 4),
                "limit_pct": limit_pct,
                "within_limit": within,
                "closed_at": closed_at,
            })
        return rows

    def advance_to(self, now: datetime) -> list[dict]:
        """Close every month/period window that ended at or before ``now``.

        Returns the history rows that were closed (also kept in memory and queued for
        persistence). Safe to call often; it does nothing until a boundary is crossed.
        """
        now = _utc(now)
        closed: list[dict] = []
        while now >= self.month_end:
            end = self.month_end
            rows = self._window_rows("MONTH", self.month_start, end, "floors_month",
                                     self.config.monthly_limit_pct, end)
            closed.extend(rows)
            self.month_history.extend(rows)
            for car in self.cars.values():
                car.floors_month = car.starts_month = car.doors_month = 0
            if end >= self.period_end:
                prows = self._window_rows("PERIOD", self.period_start, end, "floors_period",
                                          self.config.period_limit_pct, end)
                closed.extend(prows)
                self.period_history.extend(prows)
                # "Trip counter will be reset every 6 months."
                for car in self.cars.values():
                    car.floors_period = car.starts_period = car.doors_period = 0
                self.period_start = end
            self.month_start = end
        if closed:
            self.unsynced_history.extend(closed)
            # Keep memory bounded on long-running servers (DAT-2 retention lives in the DB).
            self.month_history = self.month_history[-48:]
            self.period_history = self.period_history[-16:]
        return closed

    # ------------------------------------------------------------- balancing
    def usage_penalties(self, candidate_ids: Iterable[str]) -> dict[str, float]:
        """Seconds to add to each candidate's predicted service time (FR-1.4 as amended).

        penalty_i = min(cap, gain * (d_i - d_min) / (limit * R)),  R = max(d_max, reference)

        d is the month-to-date floor counter, and d_min/d_max are taken over the eligible
        candidates of the same group, so a car that was out of service (low counter) is
        preferred when it comes back and catches up, without punishing the others for
        each other's differences.
        """
        ids = list(candidate_ids)
        penalties = {eid: 0.0 for eid in ids}
        if not self.config.balancing_enabled or len(ids) < 2:
            return penalties
        by_group: dict[str, list[str]] = {}
        for eid in ids:
            by_group.setdefault(self.group_of(eid), []).append(eid)
        limit = max(1e-6, self.config.monthly_limit_pct / 100.0)
        for members in by_group.values():
            if len(members) < 2:
                continue
            dists = {eid: self.cars[eid].floors_month for eid in members}
            low = min(dists.values())
            ref = max(max(dists.values()), self.config.reference_floors)
            for eid, dist in dists.items():
                excess = (dist - low) / (limit * ref)
                penalties[eid] = min(self.config.max_penalty_seconds, self.config.gain_seconds * excess)
        return penalties

    # ----------------------------------------------------------------- report
    def _group_summary(self, group: str) -> dict:
        members = self.group_members(group)
        month_vals = [self.cars[eid].floors_month for eid in members]
        period_vals = [self.cars[eid].floors_period for eid in members]
        m_spread = spread_pct(month_vals)
        p_spread = spread_pct(period_vals)
        single = len(members) < 2
        return {
            "group": group,
            "members": members,
            "balanced_by_dispatch": not single,
            "month_to_date": {
                "min_floors": min(month_vals) if month_vals else 0,
                "max_floors": max(month_vals) if month_vals else 0,
                "spread_pct": round(m_spread, 4),
                "limit_pct": self.config.monthly_limit_pct,
                "within_limit": single or m_spread < self.config.monthly_limit_pct,
            },
            "period_to_date": {
                "min_floors": min(period_vals) if period_vals else 0,
                "max_floors": max(period_vals) if period_vals else 0,
                "spread_pct": round(p_spread, 4),
                "limit_pct": self.config.period_limit_pct,
                "within_limit": single or p_spread <= self.config.period_limit_pct,
            },
        }

    def status(self, now: datetime | None = None) -> dict:
        def _iso(row: dict) -> dict:
            out = dict(row)
            for key in ("window_start", "window_end", "closed_at"):
                if isinstance(out.get(key), datetime):
                    out[key] = out[key].isoformat()
            return out

        elapsed = None
        if now is not None:
            total = (self.month_end - self.month_start).total_seconds()
            elapsed = round(max(0.0, min(1.0, (_utc(now) - self.month_start).total_seconds() / total)), 4)
        return {
            "requirement": "WEAR-1..WEAR-6 (instructor NFR, phase 2): floors travelled per unit; "
                           "monthly spread < 2 %, 6-month spread <= 5 %, counters reset every 6 months",
            "spread_definition": "(max_floors - min_floors) / max_floors * 100, per dispatch group",
            "config": {k: v for k, v in asdict(self.config).items() if k != "groups"}
            | {"groups": {g: list(m) for g, m in self.config.groups.items()}},
            "month": {"start": self.month_start.isoformat(), "end": self.month_end.isoformat(),
                      "elapsed_fraction": elapsed},
            "period": {"start": self.period_start.isoformat(), "end": self.period_end.isoformat(),
                       "next_counter_reset": self.period_end.isoformat()},
            "cars": [asdict(car) for car in self.cars.values()],
            "groups": [self._group_summary(g) for g in self.config.groups],
            "month_history": [_iso(r) for r in self.month_history[-12:]],
            "period_history": [_iso(r) for r in self.period_history[-4:]],
        }

    # ------------------------------------------------------------ persistence
    def counter_rows(self, updated_at: datetime) -> list[dict]:
        return [
            asdict(car) | {
                "period_start": self.period_start,
                "month_start": self.month_start,
                "updated_at": _utc(updated_at),
            }
            for car in self.cars.values()
        ]

    def restore(self, counter_rows: list[dict], history_rows: list[dict], now: datetime) -> None:
        """Restore counters persisted by ``counter_rows`` and roll forward to ``now``."""
        if counter_rows:
            first = counter_rows[0]
            self.month_start = month_floor(first["month_start"])
            self.period_start = month_floor(first["period_start"])
            for row in counter_rows:
                eid = str(row["elevator_id"])
                if eid not in self.cars:
                    continue
                car = self.cars[eid]
                for name in ("floors_month", "floors_period", "floors_lifetime",
                             "starts_month", "starts_period", "starts_lifetime",
                             "doors_month", "doors_period", "doors_lifetime"):
                    if row.get(name) is not None:
                        setattr(car, name, int(row[name]))
        for row in sorted(history_rows, key=lambda r: _utc(r["window_start"])):
            target = self.month_history if row.get("window_type") == "MONTH" else self.period_history
            target.append(dict(row))
        self.month_history = self.month_history[-48:]
        self.period_history = self.period_history[-16:]
        self.advance_to(now)
