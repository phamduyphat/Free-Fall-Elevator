"""Named repeatable traffic profiles for PERF-8, PERF-9, PERF-10, OR-10, and VT-23."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random
from typing import Literal

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

