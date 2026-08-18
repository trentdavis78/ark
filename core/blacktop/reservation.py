"""F6 — Dynamic Reservation Rate.

The accept/decline threshold F1 compares against. Not a constant, not a user
setting: an optimal-stopping policy computed from the live offer stream.

Model
-----
Offers arrive as a Poisson-ish stream with rate ``lam`` (offers/min) for the
current hex cluster x hour x weather bucket, observed over a rolling 20-40
minute window. Each offer has value ``x`` (dollars, tip-adjusted) and occupies
``d`` minutes of the driver's time (the full five-term T_total from F1).

For a threshold policy "accept iff x/d >= w", renewal-reward gives the long-run
earning rate ($/min):

    R(w) = E[x | accept] / ( 1/(lam * p(w)) + E[d | accept] )

where ``p(w)`` is the acceptance probability under the empirical value
distribution. The optimal reservation rate ``w*`` maximizes R and satisfies the
indifference condition ``w* = R(w*)`` — an offer is worth taking exactly when
its rate beats the rate obtainable by declining and continuing to search.
We compute w* by scanning candidate thresholds at the observed offer rates
(R is piecewise between order statistics, so the max is attained at one).

Layers applied on top of the raw optimum (PRD F6):
  * session-end decay — no future to wait for, so w decays toward the floor
    as planned session end approaches;
  * AR-farming policy layer — in sparse zones where Priority Access changes
    offer flow, temporarily lower w to farm acceptance rate near month end;
  * guardrails — completion-rate protection (never advise an accept that
    cannot be completed) lives in ``completion_risk``;
  * sanity band clamp — w* must land in $0.30-$0.85 per projected minute;
    leaving the band flags an upstream problem.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SANITY_BAND_PER_MIN = (0.30, 0.85)
DEFAULT_WINDOW_MINUTES = 30.0          # PRD: 20-40 minute rolling window
DEFAULT_DECAY_HORIZON_MINUTES = 45.0   # start decaying w inside this horizon


@dataclass(frozen=True)
class OfferObservation:
    """One observed offer (accepted OR declined — both are observations)."""
    seen_at: datetime
    value_dollars: float          # tip-adjusted expected payout
    duration_minutes: float       # projected T_total

    @property
    def rate_per_min(self) -> float:
        if self.duration_minutes <= 0:
            return 0.0
        return self.value_dollars / self.duration_minutes


@dataclass(frozen=True)
class ARPolicyInputs:
    """Acceptance-rate targeting inputs (PRD F6 policy constraints)."""
    current_acceptance_rate: float        # 0..1
    target_acceptance_rate: float         # e.g. 0.50 for Priority Access
    days_until_month_end: int
    zone_is_sparse: bool                  # AR only matters where PA changes flow


@dataclass(frozen=True)
class RateDecision:
    """The reservation rate with its full derivation, for display (PRD 9.5:
    never show a number without what it's compared against)."""
    w_star_per_min: float                 # final, post-layers, post-clamp
    hourly: float                         # w_star * 60
    raw_w_per_min: float                  # optimal-stopping solution pre-layers
    decay_factor: float
    ar_adjustment_per_min: float          # <= 0
    clamped: bool                         # True => left the sanity band (upstream bug signal)
    arrival_rate_per_min: float
    sample_n: int
    notes: tuple[str, ...] = ()


class RollingOfferWindow:
    """Rolling estimate of the offer value distribution and arrival rate for
    the current context. Feed every offer seen — declines included."""

    def __init__(self, window_minutes: float = DEFAULT_WINDOW_MINUTES) -> None:
        if not (15.0 <= window_minutes <= 60.0):
            raise ValueError("window_minutes should be in [15, 60]")
        self.window = timedelta(minutes=window_minutes)
        self._obs: deque[OfferObservation] = deque()

    def observe(self, obs: OfferObservation) -> None:
        self._obs.append(obs)
        self._evict(obs.seen_at)

    def _evict(self, now: datetime) -> None:
        cutoff = now - self.window
        while self._obs and self._obs[0].seen_at < cutoff:
            self._obs.popleft()

    def snapshot(self, now: datetime) -> list[OfferObservation]:
        self._evict(now)
        return list(self._obs)

    def arrival_rate_per_min(self, now: datetime) -> float:
        obs = self.snapshot(now)
        return len(obs) / self.window.total_seconds() * 60.0

    def __len__(self) -> int:
        return len(self._obs)


def solve_w_star(observations: list[OfferObservation],
                 arrival_rate_per_min: float) -> float:
    """Solve the optimal-stopping fixed point on the empirical distribution.

    Scans thresholds at each observed offer rate; returns the earning rate
    R(w_opt) at the maximizing threshold, which equals w* at the indifference
    point. Returns 0.0 when there is nothing to go on (caller falls back to
    priors / the sanity band floor).
    """
    obs = [o for o in observations if o.duration_minutes > 0]
    if not obs or arrival_rate_per_min <= 0:
        return 0.0
    rates = sorted({o.rate_per_min for o in obs})
    best = 0.0
    for w in rates:
        acc = [o for o in obs if o.rate_per_min >= w]
        p = len(acc) / len(obs)
        if p == 0.0:
            continue
        mean_x = sum(o.value_dollars for o in acc) / len(acc)
        mean_d = sum(o.duration_minutes for o in acc) / len(acc)
        expected_wait = 1.0 / (arrival_rate_per_min * p)
        r = mean_x / (expected_wait + mean_d)
        best = max(best, r)
    return best


def session_end_decay(remaining_minutes: float | None,
                      horizon_minutes: float = DEFAULT_DECAY_HORIZON_MINUTES) -> float:
    """Decay factor in (0, 1]. Inside the final ``horizon_minutes`` of a
    planned session the reservation rate decays linearly toward the floor —
    there's no future to wait for. ``None`` (open-ended session) => 1.0."""
    if remaining_minutes is None:
        return 1.0
    if remaining_minutes >= horizon_minutes:
        return 1.0
    return max(0.0, remaining_minutes / horizon_minutes)


def ar_farming_adjustment(w_per_min: float, ar: ARPolicyInputs | None) -> tuple[float, str | None]:
    """Temporarily lower w* to farm acceptance rate (Priority Access), only
    where it matters: sparse zones, near month end, AR below target.
    Reduction scales with the deficit, capped at 25% of w."""
    if ar is None or not ar.zone_is_sparse:
        return 0.0, None
    deficit = ar.target_acceptance_rate - ar.current_acceptance_rate
    if deficit <= 0 or ar.days_until_month_end > 7:
        return 0.0, None
    scale = min(1.0, deficit / 0.10)          # a 10-point deficit => full effect
    adj = -min(0.25 * w_per_min, 0.25 * w_per_min * scale)
    return adj, (f"AR farming: {ar.current_acceptance_rate:.0%} vs target "
                 f"{ar.target_acceptance_rate:.0%}, {ar.days_until_month_end}d left in month")


def completion_risk(projected_minutes: float,
                    remaining_session_minutes: float | None,
                    grace_minutes: float = 20.0) -> bool:
    """Guardrail: True when accepting an offer likely cannot be completed
    within the planned session (risking an unassign / completion-rate hit).
    F1 downgrades any GREEN to RED when this fires — completion rate >= 95%
    is release-blocking (PRD 4)."""
    if remaining_session_minutes is None:
        return False
    return projected_minutes > remaining_session_minutes + grace_minutes


class ReservationRateEngine:
    """Maintains the rolling window and produces the layered RateDecision."""

    def __init__(self,
                 window_minutes: float = DEFAULT_WINDOW_MINUTES,
                 prior_w_per_min: float = 0.50,
                 min_samples: int = 5,
                 sanity_band: tuple[float, float] = SANITY_BAND_PER_MIN) -> None:
        self.window = RollingOfferWindow(window_minutes)
        self.prior_w_per_min = prior_w_per_min
        self.min_samples = min_samples
        self.sanity_band = sanity_band

    def observe(self, obs: OfferObservation) -> None:
        self.window.observe(obs)

    def current_rate(self,
                     now: datetime,
                     remaining_session_minutes: float | None = None,
                     ar: ARPolicyInputs | None = None) -> RateDecision:
        obs = self.window.snapshot(now)
        lam = self.window.arrival_rate_per_min(now)
        notes: list[str] = []

        if len(obs) < self.min_samples:
            raw = self.prior_w_per_min
            notes.append(f"cold start: {len(obs)} samples < {self.min_samples}, using prior")
        else:
            raw = solve_w_star(obs, lam)
            if raw <= 0.0:
                raw = self.prior_w_per_min
                notes.append("degenerate window, using prior")

        decay = session_end_decay(remaining_session_minutes)
        if decay < 1.0:
            notes.append(f"session-end decay x{decay:.2f}")
        w = raw * decay

        ar_adj, ar_note = ar_farming_adjustment(w, ar)
        if ar_note:
            notes.append(ar_note)
        w += ar_adj

        lo, hi = self.sanity_band
        clamped = not (lo <= w <= hi)
        if clamped:
            notes.append(f"sanity clamp: {w:.3f}/min outside [{lo}, {hi}] — check upstream")
            w = min(max(w, lo), hi)

        return RateDecision(
            w_star_per_min=w, hourly=w * 60.0, raw_w_per_min=raw,
            decay_factor=decay, ar_adjustment_per_min=ar_adj, clamped=clamped,
            arrival_rate_per_min=lam, sample_n=len(obs), notes=tuple(notes),
        )
