from __future__ import annotations

from datetime import datetime, timezone
import math
import statistics


def normalize_timestamp(ts: datetime | str) -> datetime:
    """Normalize a datetime or ISO string to naive UTC datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def calculate_percentile(values: list[float], p: float) -> float:
    """
    Calculate percentile p (0-100) using linear interpolation
    consistent with numpy.percentile and PostgreSQL percentile_cont.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1


def summarize_distribution(
    values: list[float],
) -> tuple[float, float, float, float, float, float]:
    """
    Return `(mean, median, p90, p95, min_val, max_val)` for `values`
    (all `0.0` when `values` is empty).
    """
    if not values:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    return (
        statistics.fmean(values),
        calculate_percentile(values, 50.0),
        calculate_percentile(values, 90.0),
        calculate_percentile(values, 95.0),
        min(values),
        max(values),
    )

