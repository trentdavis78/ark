"""F4 — Merchant Wait Oracle.

Per-*location* pickup-wait distribution — P50 and P90 by hour-of-week. Not
per-brand: the same chain behaves completely differently four miles apart.

Measured passively from geofence dwell (arrival at merchant polygon ->
departure); no driver input required. This module receives those dwell
observations and answers three questions:

* ``wait_estimate`` — P50/P90 for a location at an hour (feeds F1's
  T_merchant_wait), with graceful fallback when the specific hour is sparse:
  hour-of-week -> same location all hours -> global default.
* ``chronic_offender`` — flags locations to pre-decline ("runs 11 min at
  dinner, P90 18").
* ``arrival_delay_advice`` — if predicted wait is 12 min, arrive ~6 min
  later; bounded so it can never risk a lateness violation.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from .models import hour_of_week

MAX_SAMPLES_PER_BUCKET = 200
DEFAULT_WAIT_MIN = 5.0


def _quantile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolated quantile of a sorted list."""
    if not sorted_xs:
        raise ValueError("empty sample")
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


@dataclass(frozen=True)
class WaitEstimate:
    p50: float
    p90: float
    sample_n: int
    source: str          # "hour", "location", "default"


class MerchantWaitOracle:
    def __init__(self,
                 default_wait_min: float = DEFAULT_WAIT_MIN,
                 min_hour_samples: int = 4,
                 min_location_samples: int = 3) -> None:
        self.default_wait_min = default_wait_min
        self.min_hour_samples = min_hour_samples
        self.min_location_samples = min_location_samples
        # (merchant_id, hour_of_week) -> bounded dwell samples (minutes)
        self._by_hour: dict[tuple[str, int], deque[float]] = defaultdict(
            lambda: deque(maxlen=MAX_SAMPLES_PER_BUCKET))
        self._by_location: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=MAX_SAMPLES_PER_BUCKET * 4))

    # ------------------------------------------------------------ learning
    def observe_dwell(self, merchant_id: str, arrived_at: datetime,
                      departed_at: datetime) -> None:
        minutes = (departed_at - arrived_at).total_seconds() / 60.0
        if minutes < 0:
            raise ValueError("departure before arrival")
        # Clip absurd dwells (driver parked and took a break) at 60 min.
        minutes = min(minutes, 60.0)
        self._by_hour[(merchant_id, hour_of_week(arrived_at))].append(minutes)
        self._by_location[merchant_id].append(minutes)

    # ----------------------------------------------------------- inference
    def wait_estimate(self, merchant_id: str, when: datetime) -> WaitEstimate:
        hour_samples = list(self._by_hour.get((merchant_id, hour_of_week(when)), ()))
        if len(hour_samples) >= self.min_hour_samples:
            xs = sorted(hour_samples)
            return WaitEstimate(_quantile(xs, 0.5), _quantile(xs, 0.9), len(xs), "hour")
        loc_samples = list(self._by_location.get(merchant_id, ()))
        if len(loc_samples) >= self.min_location_samples:
            xs = sorted(loc_samples)
            return WaitEstimate(_quantile(xs, 0.5), _quantile(xs, 0.9), len(xs), "location")
        return WaitEstimate(self.default_wait_min, self.default_wait_min * 2.0, 0, "default")

    def chronic_offender(self, merchant_id: str, when: datetime,
                         p50_threshold: float = 10.0) -> bool:
        est = self.wait_estimate(merchant_id, when)
        return est.sample_n >= self.min_location_samples and est.p50 >= p50_threshold

    def flag_text(self, merchant_id: str, when: datetime) -> str | None:
        if not self.chronic_offender(merchant_id, when):
            return None
        est = self.wait_estimate(merchant_id, when)
        return (f"This location runs {est.p50:.0f} min at this hour, "
                f"P90 {est.p90:.0f}.")

    def arrival_delay_advice(self, merchant_id: str, when: datetime,
                             drive_minutes: float,
                             pickup_deadline_minutes: float | None = None,
                             counter_buffer_min: float = 2.0) -> float:
        """Minutes to delay departure toward the merchant. Food won't be
        ready regardless; standing at a counter is pure unpaid loss. Bounded:
        never delays past the pickup deadline (lateness violation)."""
        est = self.wait_estimate(merchant_id, when)
        delay = max(0.0, est.p50 - counter_buffer_min)
        if pickup_deadline_minutes is not None:
            latest_safe = pickup_deadline_minutes - drive_minutes - counter_buffer_min
            delay = min(delay, max(0.0, latest_safe))
        return delay
