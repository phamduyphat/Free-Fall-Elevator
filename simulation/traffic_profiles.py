"""Named repeatable traffic profiles for PERF-8, PERF-9, PERF-10, OR-10, and VT-23."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random
from typing import Any, Literal

from simulation.clock import MORNING_PEAK_BASE_TIME, OFF_PEAK_BASE_TIME
from simulation.operational import OperationalSimulator


@dataclass(frozen=True)
class TrafficProfileConfig:
    """Reproducible request-generation configuration for a named traffic profile."""

    name: Literal["PEAK", "OFF_PEAK"]
    base_time: datetime
    duration_seconds: int
    arrival_interval_seconds: int
    b1_share: float
    up_share: float
    seed: int


PEAK_PROFILE = TrafficProfileConfig(
    name="PEAK",
    base_time=MORNING_PEAK_BASE_TIME,
    duration_seconds=600,
    arrival_interval_seconds=8,
    b1_share=0.15,
    up_share=0.55,
    seed=42,
)

OFF_PEAK_PROFILE = TrafficProfileConfig(
    name="OFF_PEAK",
    base_time=OFF_PEAK_BASE_TIME,
    duration_seconds=600,
    arrival_interval_seconds=20,
    b1_share=0.10,
    up_share=0.45,
    seed=42,
)


def run_traffic_profile(
    sim: OperationalSimulator,
    config: TrafficProfileConfig,
    *,
    reset_sim: bool = True,
) -> list[str]:
    """Execute a deterministic seeded traffic profile against `OperationalSimulator`."""
    if reset_sim:
        sim.reset(base_time=config.base_time)
    rng = random.Random(config.seed)
    request_ids: list[str] = []

    for second in range(config.duration_seconds):
        if second % config.arrival_interval_seconds == 0:
            roll = rng.random()
            if roll < config.b1_share:
                origin_floor = -1
                direction = "UP"
            elif roll < config.b1_share + config.up_share * (1.0 - config.b1_share):
                origin_floor = rng.randint(1, 29)
                direction = "UP"
            else:
                origin_floor = rng.randint(2, 30)
                direction = "DOWN"

            req, _ = sim.submit_hall_call(
                origin_floor=origin_floor,
                direction=direction,
                call_type="ORDINARY",
                traffic_profile=config.name,
            )
            request_ids.append(req["request_id"])
        sim.advance(1)

    # Drain any remaining active calls so all wait times are finalized
    for _ in range(180):
        if all(r.status in ("COMPLETED", "CANCELLED", "FAILED") for r in sim.hall_calls.values()):
            break
        sim.advance(1)

    return request_ids


def inject_load_burst(
    sim: OperationalSimulator,
    count: int = 50,
    profile: str = "RANDOM",
    auto_step_seconds: int = 0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Inject a burst of realistic simulated passenger calls into OperationalSimulator."""
    rng = random.Random(seed) if seed is not None else random.Random()
    profile_upper = str(profile).upper()
    submitted = 0
    merged = 0

    for _ in range(max(1, min(1000, count))):
        roll = rng.random()
        if profile_upper == "MORNING_PEAK":
            if roll < 0.70:
                origin = rng.randint(2, 30)
                direction = "DOWN"
            elif roll < 0.90:
                origin = -1 if rng.random() < 0.25 else 1
                direction = "UP"
            else:
                origin = rng.randint(2, 29)
                direction = "UP"
            call_type = "ORDINARY"
        elif profile_upper == "EVENING_PEAK":
            if roll < 0.70:
                origin = -1 if rng.random() < 0.25 else 1
                direction = "UP"
            elif roll < 0.90:
                origin = rng.randint(2, 30)
                direction = "DOWN"
            else:
                origin = rng.randint(2, 30)
                direction = "DOWN"
            call_type = "ORDINARY"
        elif profile_upper == "FREIGHT":
            if roll < 0.45 and sim.cars["E5"].availability == "IN_SERVICE":
                origin = rng.randint(1, 30)
                direction = None
                call_type = "FREIGHT"
            else:
                origin = rng.randint(1, 30)
                direction = "UP" if origin < 15 else "DOWN"
                call_type = "ORDINARY"
        elif profile_upper == "OFF_PEAK":
            origin = rng.randint(-1, 30)
            if origin == 0:
                origin = 1
            if origin == -1:
                direction = "UP"
            elif origin == 30:
                direction = "DOWN"
            else:
                direction = "UP" if rng.random() < 0.5 else "DOWN"
            call_type = "ORDINARY"
        else:  # RANDOM / STRESS
            origin = rng.randint(-1, 30)
            if origin == 0:
                origin = 1
            if origin == -1:
                direction = "UP"
            elif origin == 30:
                direction = "DOWN"
            else:
                direction = "UP" if rng.random() < 0.5 else "DOWN"
            call_type = "ORDINARY"

        try:
            _, is_merged = sim.submit_hall_call(
                origin_floor=origin,
                direction=direction,  # type: ignore[arg-type]
                call_type=call_type,  # type: ignore[arg-type]
                traffic_profile=profile_upper if profile_upper in ("PEAK", "OFF_PEAK") else "NORMAL",
            )
            submitted += 1
            if is_merged:
                merged += 1
        except Exception:
            pass

    if auto_step_seconds > 0:
        sim.advance(auto_step_seconds)

    active_calls = sum(
        1 for r in sim.hall_calls.values() if r.status not in ("COMPLETED", "CANCELLED", "FAILED")
    )

    return {
        "requested_count": count,
        "submitted": submitted,
        "merged": merged,
        "new_requests": submitted - merged,
        "active_hall_calls": active_calls,
        "advanced_seconds": auto_step_seconds,
        "profile": profile_upper,
    }


