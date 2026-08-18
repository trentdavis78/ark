"""F13 — Policy Self-Experimentation: threshold counterfactual replay.

Because every offer is logged including declines (each with a predicted total
and predicted minutes at decision time, and realized values when accepted),
the counterfactual "what if my threshold had been X" is computable directly
from the log, without live experimentation.

Replay semantics: offers are processed in time order against a simulated
driver who is either idle or busy. An offer arriving while busy is missed
(you can't accept what you don't see). An idle driver accepts iff the offer's
$/projected-minute clears the candidate threshold, then is busy for the
offer's minutes. Realized hourly = total earnings / total wall time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class LoggedOffer:
    """One row of the offer log (accepts and declines alike)."""
    seen_at: datetime
    value_dollars: float          # actual payout if known, else predicted total
    minutes: float                # actual total minutes if known, else predicted
    was_accepted: bool = False    # what the driver actually did (for effective-threshold inference)

    @property
    def rate_per_min(self) -> float:
        return self.value_dollars / self.minutes if self.minutes > 0 else 0.0


@dataclass(frozen=True)
class ReplayResult:
    threshold_per_min: float
    accepted: int
    missed_while_busy: int
    declined: int
    total_earnings: float
    busy_minutes: float
    wall_minutes: float

    @property
    def hourly(self) -> float:
        if self.wall_minutes <= 0:
            return 0.0
        return self.total_earnings / self.wall_minutes * 60.0


def replay(offers: list[LoggedOffer], threshold_per_min: float) -> ReplayResult:
    """Simulate the threshold policy over the logged stream."""
    ordered = sorted(offers, key=lambda o: o.seen_at)
    if not ordered:
        return ReplayResult(threshold_per_min, 0, 0, 0, 0.0, 0.0, 0.0)

    free_at = ordered[0].seen_at
    earnings = 0.0
    busy = 0.0
    accepted = missed = declined = 0
    for o in ordered:
        if o.seen_at < free_at:
            missed += 1
            continue
        if o.minutes > 0 and o.rate_per_min >= threshold_per_min:
            accepted += 1
            earnings += o.value_dollars
            busy += o.minutes
            free_at = o.seen_at + timedelta(minutes=o.minutes)
        else:
            declined += 1
    end = max(free_at, ordered[-1].seen_at)
    wall = (end - ordered[0].seen_at).total_seconds() / 60.0
    return ReplayResult(threshold_per_min, accepted, missed, declined,
                        earnings, busy, max(wall, busy))


def sweep(offers: list[LoggedOffer],
          thresholds: list[float]) -> list[ReplayResult]:
    return [replay(offers, t) for t in sorted(thresholds)]


def effective_threshold(offers: list[LoggedOffer]) -> float | None:
    """Infer the threshold the driver actually applied: the cut minimizing
    disagreement with their realized accept/decline decisions."""
    rated = [o for o in offers if o.minutes > 0]
    if not rated or not any(o.was_accepted for o in rated) \
            or all(o.was_accepted for o in rated):
        return None
    candidates = sorted({o.rate_per_min for o in rated})
    best_t, best_err = None, None
    for t in candidates:
        err = sum(1 for o in rated
                  if (o.rate_per_min >= t) != o.was_accepted)
        if best_err is None or err < best_err:
            best_t, best_err = t, err
    return best_t


def weekly_report(offers: list[LoggedOffer],
                  thresholds: list[float] | None = None) -> str:
    """The PRD F13 narrative: effective threshold, realized hourly, and the
    counterfactual table with percentage deltas."""
    eff = effective_threshold(offers)
    if eff is None:
        return "Not enough decision history for a counterfactual report yet."
    base = replay(offers, eff)
    if thresholds is None:
        thresholds = sorted({round(eff + d, 2) for d in (-0.14, -0.07, 0.0, 0.07, 0.14)})
    lines = [f"Your effective threshold was ${eff:.2f}/min, producing ${base.hourly:.2f}/hr."]
    for r in sweep(offers, thresholds):
        if abs(r.threshold_per_min - eff) < 1e-9:
            continue
        delta = (r.hourly - base.hourly) / base.hourly * 100.0 if base.hourly else 0.0
        lines.append(f"Simulated at ${r.threshold_per_min:.2f}: ${r.hourly:.2f}/hr "
                     f"({delta:+.1f}%) — {r.accepted} accepts, {r.missed_while_busy} missed while busy.")
    return "\n".join(lines)
