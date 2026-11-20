"""Wear and tear balancing algorithm and reproducible 6-month benchmark simulation.

Implements non-functional requirements for elevator controller balancing:
- Balances accumulated distance (number of floors traversed) across residential units E1-E4.
- Maximum difference (spread between min and max distances) kept under 1.8% per month
  (strict safety margin under the 2.0% monthly ceiling).
- Maximum difference kept under 4.0% in 6 months
  (strict safety margin under the 5.0% 6-month ceiling).
- Dual trip & distance counters:
  - 1-month reset counter (resets every 30 days).
  - 6-month reset counter (resets every 180 days).

Supported configurable strategies:
1. BOTTOM_PRIORITY: Elevators at bottom threshold get priority until within 0.2% safety margin.
2. TOP_HALT: Elevators at top threshold halt/pause until within 0.2% safety margin.
3. LEAST_RUN_FIRST: Least-run elevator is always prioritized for incoming assignments.
4. OFF_PEAK_REBALANCE: Mileage is actively rebalanced during off-peak hours (time configurable).
5. OUTLIER_CORRECTION: Detects statistical outliers (high or low) and prioritizes or halts them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import math
import random
from typing import Any, Literal

WearTearStrategyName = Literal[
    "BOTTOM_PRIORITY",
    "TOP_HALT",
    "LEAST_RUN_FIRST",
    "OFF_PEAK_REBALANCE",
    "OUTLIER_CORRECTION",
]

STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "BOTTOM_PRIORITY": "1. Bottom Threshold Priority (Prioritize car until within 0.2% safety margin)",
    "TOP_HALT": "2. Top Threshold Halt (Pause car until within 0.2% safety margin)",
    "LEAST_RUN_FIRST": "3. Least-Run Elevator Priority (Most worked rests naturally)",
    "OFF_PEAK_REBALANCE": "4. Off-Peak Rebalancing (Balance during low-traffic window)",
    "OUTLIER_CORRECTION": "5. Statistical Outlier Detection (Prioritize low / Halt high outlier)",
}


@dataclass
class WearTearConfig:
    """Runtime configuration for wear-and-tear distance balancing."""

    enabled: bool = True
    strategy: WearTearStrategyName = "BOTTOM_PRIORITY"
    monthly_target_pct: float = 1.8  # Safety target under 2.0%
    monthly_ceiling_pct: float = 2.0  # Hard SRS non-functional ceiling
    six_month_target_pct: float = 4.0  # Safety target under 5.0%
    six_month_ceiling_pct: float = 5.0  # Hard SRS non-functional ceiling
    safety_margin_pct: float = 0.2  # 0.2% recovery buffer
    off_peak_start_hour: int = 22  # 22:00 (10 PM)
    off_peak_end_hour: int = 5  # 05:00 (5 AM)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WearTearConfig:
        valid_strategies = {
            "BOTTOM_PRIORITY",
            "TOP_HALT",
            "LEAST_RUN_FIRST",
            "OFF_PEAK_REBALANCE",
            "OUTLIER_CORRECTION",
        }
        strategy = str(data.get("strategy", "BOTTOM_PRIORITY")).upper()
        if strategy not in valid_strategies:
            strategy = "BOTTOM_PRIORITY"

        return cls(
            enabled=bool(data.get("enabled", True)),
            strategy=strategy,  # type: ignore[arg-type]
            monthly_target_pct=float(data.get("monthly_target_pct", 1.8)),
            monthly_ceiling_pct=float(data.get("monthly_ceiling_pct", 2.0)),
            six_month_target_pct=float(data.get("six_month_target_pct", 4.0)),
            six_month_ceiling_pct=float(data.get("six_month_ceiling_pct", 5.0)),
            safety_margin_pct=float(data.get("safety_margin_pct", 0.2)),
            off_peak_start_hour=int(data.get("off_peak_start_hour", 22)),
            off_peak_end_hour=int(data.get("off_peak_end_hour", 5)),
        )


@dataclass
class WearTearMetrics:
    """Wear-and-tear distance analysis across the residential elevator group."""

    elevator_distances: dict[str, int]
    min_distance: int = 0
    max_distance: int = 0
    mean_distance: float = 0.0
    delta_floors: int = 0
    spread_pct: float = 0.0
    monthly_target_met: bool = True
    six_month_target_met: bool = True
    outlier_car: str | None = None
    outlier_type: str | None = None  # "LOW", "HIGH", or None


def calculate_wear_metrics(
    distances: dict[str, int],
    config: WearTearConfig | None = None,
) -> WearTearMetrics:
    """Compute difference statistics and compliance against 1.8% and 4.0% targets."""
    cfg = config or WearTearConfig()
    # Wear and tear applies across residential units (E1, E2, E3, E4)
    target_cars = [k for k in ("E1", "E2", "E3", "E4") if k in distances]
    if not target_cars:
        target_cars = list(distances.keys())

    if not target_cars:
        return WearTearMetrics(elevator_distances={})

    vals = [distances[c] for c in target_cars]
    min_val = min(vals)
    max_val = max(vals)
    mean_val = sum(vals) / len(vals)
    delta = max_val - min_val

    # Relative spread between min and max distances
    spread_pct = (delta / max_val * 100.0) if max_val > 0 else 0.0

    # Outlier detection: identify if 1 unit diverges significantly from the other 3
    outlier_car, outlier_type = detect_outlier(distances, target_cars)

    monthly_met = spread_pct <= cfg.monthly_target_pct
    six_month_met = spread_pct <= cfg.six_month_target_pct

    return WearTearMetrics(
        elevator_distances={c: distances[c] for c in target_cars},
        min_distance=min_val,
        max_distance=max_val,
        mean_distance=round(mean_val, 2),
        delta_floors=delta,
        spread_pct=round(spread_pct, 3),
        monthly_target_met=monthly_met,
        six_month_target_met=six_month_met,
        outlier_car=outlier_car,
        outlier_type=outlier_type,
    )


def detect_outlier(
    distances: dict[str, int],
    target_cars: list[str],
) -> tuple[str | None, str | None]:
    """
    Detect if one car is an outlier compared to the rest of the group using median deviation.

    Returns (car_id, "LOW" | "HIGH") or (None, None).
    """
    if len(target_cars) < 3:
        return None, None

    vals = [distances[c] for c in target_cars if c in distances]
    if not vals:
        return None, None

    sorted_vals = sorted(vals)
    n = len(sorted_vals)
    med = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0 if n % 2 == 0 else float(sorted_vals[n // 2])
    if med <= 0:
        return None, None

    # Find the car with the largest deviation from the median
    max_dev = 0.0
    best_car = None
    best_type = None

    for car in target_cars:
        if car not in distances:
            continue
        d = distances[car]
        dev = d - med
        abs_dev = abs(dev)
        if abs_dev > max_dev:
            max_dev = abs_dev
            best_car = car
            best_type = "HIGH" if dev > 0 else "LOW"

    # Only flag as outlier if deviation from median is at least 1.5%
    if best_car and (max_dev / med * 100.0) >= 1.5:
        return best_car, best_type

    return None, None


def is_off_peak_hour(hour: int, start_hour: int = 22, end_hour: int = 5) -> bool:
    """Check if the given hour falls inside the configured off-peak window."""
    if start_hour > end_hour:
        return hour >= start_hour or hour < end_hour
    return start_hour <= hour < end_hour


def _workload(car: Any) -> int:
    """Return active queue depth (number of pending stops) of a car."""
    stops = getattr(car, "stops", None)
    if stops is not None and hasattr(stops, "__len__"):
        return len(stops)
    return 0


def _eta_val(c: Any, eta_scores: dict[str, Any] | None) -> int:
    """Extract integer ETA score for a car if available, handling ints and (estimate, index) tuples."""
    if not eta_scores or c.elevator_id not in eta_scores:
        return 0
    score = eta_scores[c.elevator_id]
    if isinstance(score, (tuple, list)):
        return int(score[0])
    try:
        return int(score)
    except (ValueError, TypeError):
        return 0


def _car_id_num(c: Any) -> int:
    """Extract integer car ID (e.g., 'E1' -> 1)."""
    eid = getattr(c, "elevator_id", "")
    if eid.startswith("E") and eid[1:].isdigit():
        return int(eid[1:])
    return 0


def select_best_car_for_wear(
    eligible_cars: list[Any],
    car_distances: dict[str, int],
    config: WearTearConfig,
    current_time: datetime,
    eta_scores: dict[str, int] | None = None,
) -> Any:
    """
    Select or order eligible cars applying the configured wear-and-tear strategy.

    Each car in eligible_cars must have .elevator_id.
    """
    if not config.enabled or len(eligible_cars) <= 1:
        # Fall back to lowest workload, lowest ETA, then car ID
        if eta_scores:
            return min(
                eligible_cars,
                key=lambda c: (_workload(c), _eta_val(c, eta_scores), _car_id_num(c)),
            )
        return min(eligible_cars, key=lambda c: (_workload(c), _car_id_num(c)))

    residential_eligible = [c for c in eligible_cars if c.elevator_id in ("E1", "E2", "E3", "E4")]
    if not residential_eligible:
        return eligible_cars[0]

    res_distances = {c.elevator_id: car_distances.get(c.elevator_id, 0) for c in residential_eligible}
    min_dist = min(res_distances.values())
    max_dist = max(res_distances.values())
    spread_pct = ((max_dist - min_dist) / max_dist * 100.0) if max_dist > 0 else 0.0

    # 1. STRATEGY: BOTTOM_PRIORITY
    if config.strategy == "BOTTOM_PRIORITY":
        # Target threshold safety trigger
        trigger_pct = max(0.1, config.monthly_target_pct - config.safety_margin_pct)
        if spread_pct >= trigger_pct:
            # Active violation: cars at or near min distance get strict priority
            safety_cutoff = min_dist + int(max_dist * (config.safety_margin_pct / 100.0))
            lagging_cars = [c for c in residential_eligible if res_distances[c.elevator_id] <= safety_cutoff]
            if lagging_cars:
                if eta_scores:
                    return min(
                        lagging_cars,
                        key=lambda c: (_workload(c), res_distances[c.elevator_id], _eta_val(c, eta_scores), _car_id_num(c)),
                    )
                return min(lagging_cars, key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)))

    # 2. STRATEGY: TOP_HALT
    elif config.strategy == "TOP_HALT":
        trigger_pct = max(0.1, config.monthly_target_pct - config.safety_margin_pct)
        if spread_pct >= trigger_pct:
            # Active violation: car at max distance halts/pauses taking calls if other cars are available
            safety_cutoff = max_dist - int(max_dist * (config.safety_margin_pct / 100.0))
            non_top_cars = [c for c in residential_eligible if res_distances[c.elevator_id] < safety_cutoff]
            if non_top_cars:
                if eta_scores:
                    return min(
                        non_top_cars,
                        key=lambda c: (_workload(c), _eta_val(c, eta_scores), res_distances[c.elevator_id], _car_id_num(c)),
                    )
                return min(non_top_cars, key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)))

    # 3. STRATEGY: LEAST_RUN_FIRST
    elif config.strategy == "LEAST_RUN_FIRST":
        trigger_pct = max(0.1, config.monthly_target_pct - config.safety_margin_pct)
        if spread_pct >= trigger_pct:
            # When imbalanced, sort primarily by least accumulated distance
            if eta_scores:
                return min(
                    residential_eligible,
                    key=lambda c: (res_distances[c.elevator_id], _workload(c), _eta_val(c, eta_scores), _car_id_num(c)),
                )
            return min(
                residential_eligible,
                key=lambda c: (res_distances[c.elevator_id], _workload(c), _car_id_num(c)),
            )
        # When safe, sparsely spread workload
        if eta_scores:
            return min(
                residential_eligible,
                key=lambda c: (_workload(c), res_distances[c.elevator_id], _eta_val(c, eta_scores), _car_id_num(c)),
            )
        return min(
            residential_eligible,
            key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)),
        )

    # 4. STRATEGY: OFF_PEAK_REBALANCE
    elif config.strategy == "OFF_PEAK_REBALANCE":
        is_off_peak = is_off_peak_hour(
            current_time.hour,
            config.off_peak_start_hour,
            config.off_peak_end_hour,
        )
        if is_off_peak:
            # During low-traffic off-peak hours, aggressively route to the car with lowest distance
            return min(
                residential_eligible,
                key=lambda c: (res_distances[c.elevator_id], _workload(c), _car_id_num(c)),
            )
        # Outside off-peak hours (safe / regular operation), sparsely spread workload
        if eta_scores:
            return min(
                residential_eligible,
                key=lambda c: (_workload(c), _eta_val(c, eta_scores), res_distances[c.elevator_id], _car_id_num(c)),
            )
        return min(
            residential_eligible,
            key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)),
        )

    # 5. STRATEGY: OUTLIER_CORRECTION
    elif config.strategy == "OUTLIER_CORRECTION":
        all_group_distances = {cid: car_distances.get(cid, 0) for cid in ("E1", "E2", "E3", "E4")}
        outlier_car, outlier_type = detect_outlier(all_group_distances, ["E1", "E2", "E3", "E4"])
        if outlier_car and outlier_type == "LOW":
            # Lagging car prioritized
            matching = [c for c in residential_eligible if c.elevator_id == outlier_car]
            if matching:
                return matching[0]
        elif outlier_car and outlier_type == "HIGH":
            # Leading outlier car temporarily halted/rested for maintenance
            non_outliers = [c for c in residential_eligible if c.elevator_id != outlier_car]
            if non_outliers:
                if eta_scores:
                    return min(
                        non_outliers,
                        key=lambda c: (_workload(c), _eta_val(c, eta_scores), res_distances[c.elevator_id], _car_id_num(c)),
                    )
                return min(
                    non_outliers,
                    key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)),
                )

    # Default balanced pick: when the balancing strategy is safe, sparsely spread the workload
    if eta_scores:
        return min(
            residential_eligible,
            key=lambda c: (_workload(c), res_distances[c.elevator_id], _eta_val(c, eta_scores), _car_id_num(c)),
        )
    return min(
        residential_eligible,
        key=lambda c: (_workload(c), res_distances[c.elevator_id], _car_id_num(c)),
    )


@dataclass
class BenchmarkDailyPoint:
    day: int
    date_str: str
    E1_floors: int
    E2_floors: int
    E3_floors: int
    E4_floors: int
    min_floors: int
    max_floors: int
    delta_floors: int
    spread_pct: float
    monthly_pass: bool
    six_month_pass: bool


@dataclass
class BenchmarkSimulationResult:
    """Complete results from running a multi-month benchmark with synthetic loads."""

    strategy: str
    strategy_description: str
    enabled: bool
    seed: int
    simulated_months: int
    simulated_days: int
    total_calls_generated: int
    total_floors_traversed: int
    car_floors: dict[str, int]
    car_trips: dict[str, int]
    monthly_delta_floors: int
    monthly_spread_pct: float
    monthly_target_met: bool  # <= 1.8%
    monthly_requirement_met: bool  # <= 2.0%
    six_month_delta_floors: int
    six_month_spread_pct: float
    six_month_target_met: bool  # <= 4.0%
    six_month_requirement_met: bool  # <= 5.0%
    time_series: list[BenchmarkDailyPoint]
    baseline_comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_description": self.strategy_description,
            "enabled": self.enabled,
            "seed": self.seed,
            "simulated_months": self.simulated_months,
            "simulated_days": self.simulated_days,
            "total_calls_generated": self.total_calls_generated,
            "total_floors_traversed": self.total_floors_traversed,
            "car_floors": self.car_floors,
            "car_trips": self.car_trips,
            "monthly_delta_floors": self.monthly_delta_floors,
            "monthly_spread_pct": round(self.monthly_spread_pct, 3),
            "monthly_target_met": self.monthly_target_met,
            "monthly_requirement_met": self.monthly_requirement_met,
            "six_month_delta_floors": self.six_month_delta_floors,
            "six_month_spread_pct": round(self.six_month_spread_pct, 3),
            "six_month_target_met": self.six_month_target_met,
            "six_month_requirement_met": self.six_month_requirement_met,
            "time_series": [asdict(p) for p in self.time_series],
            "baseline_comparison": self.baseline_comparison,
        }


def _simulate_run(
    seed: int,
    months: int,
    days_per_month: int,
    calls_per_day: int,
    config: WearTearConfig,
) -> tuple[
    dict[str, int],
    dict[str, int],
    dict[str, int],
    dict[str, int],
    list[BenchmarkDailyPoint],
    int,
]:
    """Internal helper to execute a deterministic synthetic traffic load."""
    rng = random.Random(seed)
    total_days = months * days_per_month
    cars = ["E1", "E2", "E3", "E4"]

    # Car positions and counters
    positions = {c: 1 for c in cars}
    floors_total = {c: 0 for c in cars}
    trips_total = {c: 0 for c in cars}
    floors_monthly = {c: 0 for c in cars}
    trips_monthly = {c: 0 for c in cars}
    floors_six_months = {c: 0 for c in cars}
    trips_six_months = {c: 0 for c in cars}

    time_series: list[BenchmarkDailyPoint] = []
    total_calls = 0

    base_date = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    for day in range(1, total_days + 1):
        # Monthly reset every 30 days
        if (day - 1) % days_per_month == 0 and day > 1:
            for c in cars:
                floors_monthly[c] = 0
                trips_monthly[c] = 0

        # 6-month reset every 180 days
        if (day - 1) % (days_per_month * 6) == 0 and day > 1:
            for c in cars:
                floors_six_months[c] = 0
                trips_six_months[c] = 0

        # Generate day's traffic
        # Distribute calls across hours: Morning Peak (07-09), Evening Peak (17-20), Normal, Off-Peak (22-05)
        for _ in range(calls_per_day):
            total_calls += 1
            hour_roll = rng.random()
            if hour_roll < 0.35:
                # Morning peak: 7:00 to 9:00 (70% down)
                hour = rng.choice([7, 8])
                if rng.random() < 0.70:
                    origin = rng.randint(2, 30)
                    dest = -1 if rng.random() < 0.25 else 1
                else:
                    dest = rng.randint(2, 30)
                    origin = -1 if rng.random() < 0.25 else 1
            elif hour_roll < 0.70:
                # Evening peak: 17:00 to 20:00 (70% up)
                hour = rng.choice([17, 18, 19])
                if rng.random() < 0.70:
                    origin = -1 if rng.random() < 0.25 else 1
                    dest = rng.randint(2, 30)
                else:
                    origin = rng.randint(2, 30)
                    dest = -1 if rng.random() < 0.25 else 1
            elif hour_roll < 0.90:
                # Normal day hours: 9:00 to 17:00
                hour = rng.randint(9, 16)
                origin = rng.randint(-1, 30)
                if origin == 0:
                    origin = 1
                dest = rng.randint(-1, 30)
                if dest == 0 or dest == origin:
                    dest = 1 if origin != 1 else 15
            else:
                # Off-peak night hours: 22:00 to 5:00
                hour = rng.choice([22, 23, 0, 1, 2, 3, 4])
                origin = rng.randint(-1, 30)
                if origin == 0:
                    origin = 1
                dest = rng.randint(-1, 30)
                if dest == 0 or dest == origin:
                    dest = 1 if origin != 1 else 10

            call_dt = base_date + timedelta(days=day - 1, hours=hour, minutes=rng.randint(0, 59))

            # Eligible cars
            class StubCar:
                def __init__(self, eid: str):
                    self.elevator_id = eid

            stub_cars = [StubCar(c) for c in cars]

            # Compute ETAs based on distance to pickup
            def pos(f: int) -> int:
                return 0 if f == -1 else f

            orig_pos = pos(origin)
            dest_pos = pos(dest)
            eta_scores = {c: abs(pos(positions[c]) - orig_pos) for c in cars}

            active_distances = (
                floors_six_months
                if (config.strategy in ("BOTTOM_PRIORITY", "TOP_HALT") and day > 30)
                else floors_monthly
            )

            chosen_stub = select_best_car_for_wear(
                eligible_cars=stub_cars,
                car_distances=active_distances,
                config=config,
                current_time=call_dt,
                eta_scores=eta_scores,
            )
            chosen_id = chosen_stub.elevator_id

            # Execute trip on chosen car
            trip_floors = abs(pos(positions[chosen_id]) - orig_pos) + abs(orig_pos - dest_pos)
            positions[chosen_id] = dest

            floors_total[chosen_id] += trip_floors
            trips_total[chosen_id] += 1
            floors_monthly[chosen_id] += trip_floors
            trips_monthly[chosen_id] += 1
            floors_six_months[chosen_id] += trip_floors
            trips_six_months[chosen_id] += 1

        # End of day snapshot
        day_date = (base_date + timedelta(days=day - 1)).strftime("%Y-%m-%d")
        daily_vals = [floors_six_months[c] for c in cars]
        d_min = min(daily_vals)
        d_max = max(daily_vals)
        d_delta = d_max - d_min
        d_spread = (d_delta / d_max * 100.0) if d_max > 0 else 0.0

        monthly_vals = [floors_monthly[c] for c in cars]
        m_min = min(monthly_vals)
        m_max = max(monthly_vals)
        m_spread = ((m_max - m_min) / m_max * 100.0) if m_max > 0 else 0.0

        time_series.append(
            BenchmarkDailyPoint(
                day=day,
                date_str=day_date,
                E1_floors=floors_six_months["E1"],
                E2_floors=floors_six_months["E2"],
                E3_floors=floors_six_months["E3"],
                E4_floors=floors_six_months["E4"],
                min_floors=d_min,
                max_floors=d_max,
                delta_floors=d_delta,
                spread_pct=round(d_spread, 3),
                monthly_pass=m_spread <= config.monthly_target_pct,
                six_month_pass=d_spread <= config.six_month_target_pct,
            )
        )

    return (
        floors_total,
        trips_total,
        floors_monthly,
        floors_six_months,
        time_series,
        total_calls,
    )


def run_wear_tear_benchmark(
    seed: int = 42,
    months: int = 6,
    days_per_month: int = 30,
    calls_per_day: int = 240,
    config: WearTearConfig | None = None,
    compare_baseline: bool = True,
) -> BenchmarkSimulationResult:
    """
    Execute a reproducible 6-month simulated load benchmark.

    Tests that accumulated floor distances across E1-E4 satisfy:
    - Monthly difference <= 1.8% (safety target for 2.0% ceiling).
    - 6-month difference <= 4.0% (safety target for 5.0% ceiling).
    """
    cfg = config or WearTearConfig()

    (
        floors_total,
        trips_total,
        floors_monthly,
        floors_six_months,
        time_series,
        total_calls,
    ) = _simulate_run(
        seed=seed,
        months=months,
        days_per_month=days_per_month,
        calls_per_day=calls_per_day,
        config=cfg,
    )

    cars = ["E1", "E2", "E3", "E4"]
    m_vals = [floors_monthly[c] for c in cars]
    m_min, m_max = min(m_vals), max(m_vals)
    m_delta = m_max - m_min
    m_spread = (m_delta / m_max * 100.0) if m_max > 0 else 0.0

    s_vals = [floors_six_months[c] for c in cars]
    s_min, s_max = min(s_vals), max(s_vals)
    s_delta = s_max - s_min
    s_spread = (s_delta / s_max * 100.0) if s_max > 0 else 0.0

    baseline_data: dict[str, Any] | None = None
    if compare_baseline and cfg.enabled:
        unmitigated_cfg = WearTearConfig(enabled=False)
        (
            b_floors_total,
            b_trips_total,
            b_floors_monthly,
            b_floors_six_months,
            _,
            _,
        ) = _simulate_run(
            seed=seed,
            months=months,
            days_per_month=days_per_month,
            calls_per_day=calls_per_day,
            config=unmitigated_cfg,
        )
        bm_vals = [b_floors_monthly[c] for c in cars]
        bs_vals = [b_floors_six_months[c] for c in cars]
        bm_spread = ((max(bm_vals) - min(bm_vals)) / max(bm_vals) * 100.0) if max(bm_vals) > 0 else 0.0
        bs_spread = ((max(bs_vals) - min(bs_vals)) / max(bs_vals) * 100.0) if max(bs_vals) > 0 else 0.0

        baseline_data = {
            "unmitigated_car_floors": b_floors_six_months,
            "unmitigated_monthly_spread_pct": round(bm_spread, 3),
            "unmitigated_six_month_spread_pct": round(bs_spread, 3),
            "improvement_pct_points": round(bs_spread - s_spread, 3),
        }

    return BenchmarkSimulationResult(
        strategy=cfg.strategy,
        strategy_description=STRATEGY_DESCRIPTIONS.get(cfg.strategy, cfg.strategy),
        enabled=cfg.enabled,
        seed=seed,
        simulated_months=months,
        simulated_days=months * days_per_month,
        total_calls_generated=total_calls,
        total_floors_traversed=sum(floors_total.values()),
        car_floors=floors_six_months,
        car_trips=trips_total,
        monthly_delta_floors=m_delta,
        monthly_spread_pct=round(m_spread, 3),
        monthly_target_met=m_spread <= cfg.monthly_target_pct,
        monthly_requirement_met=m_spread <= cfg.monthly_ceiling_pct,
        six_month_delta_floors=s_delta,
        six_month_spread_pct=round(s_spread, 3),
        six_month_target_met=s_spread <= cfg.six_month_target_pct,
        six_month_requirement_met=s_spread <= cfg.six_month_ceiling_pct,
        time_series=time_series,
        baseline_comparison=baseline_data,
    )
