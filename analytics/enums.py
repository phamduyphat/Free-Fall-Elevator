from __future__ import annotations

from enum import StrEnum

ELEVATOR_IDS: list[str] = ["E1", "E2", "E3", "E4", "E5"]


class TrafficProfile(StrEnum):
    PEAK = "PEAK"
    OFF_PEAK = "OFF_PEAK"
    NORMAL = "NORMAL"
    STRESS = "STRESS"

