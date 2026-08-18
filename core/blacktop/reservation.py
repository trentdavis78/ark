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
from typing import Callable

SANITY_BAND_PER_MIN = (0.30, 0.85)
DEFAULT_WINDOW_MINUTES = 30.0          # PRD: 20-40 minute rolling window
DEFAULT_DECAY_HORIZON_MINUTES = 45.0   # start decaying w inside this horizon
MIN_AVAILABLE_MINUTES = 1.0            # floor on the lambda denominator


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
    out_of_band: bool                     # left the sanity band => upstream bug signal
    floor_applied: bool                   # marginal-cost floor bound the rate
    arrival_rate_per_min: float
    sample_n: int
    notes: tuple[str, ...] = ()


class RollingOfferWindow:
    """Rolling estimate of the offer value distribution and arrival rate for
    the current context. Feed every offer seen — declines included.

    Starvation
    ----------
    A working driver only *sees* offers while idle, so at high utilization a
    strict 20-40 minute wall window holds two or three observations and the
    optimal-stopping solve never has enough data to run. Reaching for a
    constant prior in that state silently turns the engine back into a fixed
    threshold, which is the failure mode this whole module exists to avoid.

    So the window is time-bounded *and* sample-bounded: it keeps recent
    history up to ``retain_minutes`` and, when the nominal window is thin,
    reaches further back for up to ``min_samples`` observations. ``span_minutes``
    reports the true elapsed span of whatever was returned, so the arrival
    rate is always computed over the period the samples actually cover.
    """

    def __init__(self, window_minutes: float = DEFAULT_WINDOW_MINUTES,
                 min_samples: int = 5,
                 retain_minutes: float = 240.0) -> None:
        if not (15.0 <= window_minutes <= 60.0):
            raise ValueError("window_minutes should be in [15, 60]")
        self.window = timedelta(minutes=window_minutes)
        self.retain = timedelta(minutes=max(retain_minutes, window_minutes))
        self.min_samples = min_samples
        self._obs: deque[OfferObservation] = deque()
        self._unavailable: deque[tuple[datetime, datetime]] = deque()

    def observe(self, obs: OfferObservation) -> None:
        self._obs.append(obs)
        self._evict(obs.seen_at)

    def note_unavailable(self, start: datetime, end: datetime) -> None:
        """Record an interval the driver was on a delivery and could not be
        offered work. This is the denominator correction for ``lambda``: it is
        the driver's own session timeline, not platform data (I5)."""
        if end > start:
            self._unavailable.append((start, end))

    def _evict(self, now: datetime) -> None:
        """Drop beyond the retention horizon, never merely beyond the window —
        the extra history is what rescues a starved window."""
        cutoff = now - self.retain
        while self._obs and self._obs[0].seen_at < cutoff:
            self._obs.popleft()
        while self._unavailable and self._unavailable[0][1] < cutoff:
            self._unavailable.popleft()

    def snapshot(self, now: datetime) -> list[OfferObservation]:
        self._evict(now)
        cutoff = now - self.window
        fresh = [o for o in self._obs if o.seen_at >= cutoff]
        if len(fresh) >= self.min_samples:
            return fresh
        # Thin: reach back for the most recent min_samples, however old.
        return list(self._obs)[-self.min_samples:]

    def _span_start(self, now: datetime) -> datetime:
        """Start of the period ``snapshot`` actually covers: the nominal window
        when fresh data suffices, extended when the window had to reach back."""
        obs = self.snapshot(now)
        nominal_start = now - self.window
        if len(obs) < 2:
            return nominal_start
        return min(obs[0].seen_at, nominal_start)

    def span_minutes(self, now: datetime) -> float:
        return max((now - self._span_start(now)).total_seconds() / 60.0, MIN_AVAILABLE_MINUTES)

    def available_minutes(self, now: datetime) -> float:
        """Minutes in the span during which an offer could actually have been
        received — elapsed time minus time spent on deliveries."""
        start = self._span_start(now)
        elapsed = (now - start).total_seconds() / 60.0
        busy = 0.0
        for b0, b1 in self._unavailable:
            lo, hi = max(b0, start), min(b1, now)
            if hi > lo:
                busy += (hi - lo).total_seconds() / 60.0
        return max(elapsed - busy, MIN_AVAILABLE_MINUTES)

    def arrival_rate_per_min(self, now: datetime) -> float:
        """Offers seen per minute *available*.

        Dividing by elapsed time instead understates lambda for any driver who
        is actually working: offers arriving mid-delivery are never seen. That
        understatement lengthens the modeled wait for a better draw, depresses
        the reservation rate, and makes the engine accept offers it should
        decline -- measured at roughly 15 points of excess acceptance in every
        zone before this correction. The busy intervals come from the driver's
        own session timeline, so no platform data is involved (I2, I5).
        """
        obs = self.snapshot(now)
        if not obs:
            return 0.0
        return len(obs) / self.available_minutes(now)

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
    """Maintains the rolling windows and produces the layered RateDecision.

    Context
    -------
    PRD F6 scopes the rolling estimate to "the current hex cluster x hour x
    weather bucket". That scoping is load-bearing, not decorative: a single
    global window lets a dense corridor's offer distribution set the threshold
    for a sparse one, and the sparse zone then declines everything it is
    offered. Windows are therefore keyed by context string. Hour is left to
    recency -- a 20-40 minute window is inside one hour by construction --
    so callers key on hex cluster and weather.
    """

    def __init__(self,
                 window_minutes: float = DEFAULT_WINDOW_MINUTES,
                 prior_w_per_min: float = 0.50,
                 min_samples: int = 5,
                 shrinkage_k: float = 4.0,
                 prior_provider: Callable[[datetime], float | None] | None = None,
                 sanity_band: tuple[float, float] = SANITY_BAND_PER_MIN,
                 marginal_cost_per_min: float = 0.0) -> None:
        self.window_minutes = window_minutes
        self._windows: dict[str, RollingOfferWindow] = {}
        self.prior_w_per_min = prior_w_per_min
        self.min_samples = min_samples
        # Partial pooling toward the prior, the same shrinkage F2 uses. A hard
        # cold-start switch to a constant is what starved the rate in practice;
        # pooling degrades smoothly and washes the prior out as data arrives.
        self.shrinkage_k = shrinkage_k
        # Context-specific prior (hex x hour x weather) learned from the
        # driver's own history. Falls back to the constant when unavailable.
        self.prior_provider = prior_provider
        # Diagnostic only. PRD F6 states the band as an expectation about
        # where w* should land -- "if it leaves that band, something is wrong
        # upstream" -- not as a policy override. Clamping w* to a constant
        # would silently reinstate the fixed threshold this engine exists to
        # replace, and in a genuinely lean market it declines everything.
        self.sanity_band = sanity_band
        # The one economically correct floor: never recommend an offer that
        # does not clear the marginal cost of driving it. Opportunity cost of
        # waiting is already inside the optimal-stopping solution.
        self.marginal_cost_per_min = marginal_cost_per_min

    @property
    def window(self) -> RollingOfferWindow:
        """The default-context window (single-context callers and tests)."""
        return self._window_for("")

    def _window_for(self, context: str) -> RollingOfferWindow:
        win = self._windows.get(context)
        if win is None:
            win = RollingOfferWindow(self.window_minutes, min_samples=self.min_samples)
            self._windows[context] = win
        return win

    def observe(self, obs: OfferObservation, context: str = "") -> None:
        self._window_for(context).observe(obs)

    def note_unavailable(self, start: datetime, end: datetime,
                         context: str = "") -> None:
        """Mark a delivery interval so lambda is measured per minute available."""
        self._window_for(context).note_unavailable(start, end)

    def _prior_for(self, now: datetime) -> float:
        if self.prior_provider is not None:
            p = self.prior_provider(now)
            if p is not None and p > 0.0:
                return p
        return self.prior_w_per_min

    def current_rate(self,
                     now: datetime,
                     remaining_session_minutes: float | None = None,
                     ar: ARPolicyInputs | None = None,
                     context: str = "") -> RateDecision:
        win = self._window_for(context)
        obs = win.snapshot(now)
        lam = win.arrival_rate_per_min(now)
        notes: list[str] = []

        prior = self._prior_for(now)
        solved = solve_w_star(obs, lam) if len(obs) >= 2 else 0.0
        n = len(obs)
        if solved <= 0.0:
            raw = prior
            notes.append(f"cold start: {n} usable samples, using prior {prior:.3f}/min")
        else:
            raw = (n * solved + self.shrinkage_k * prior) / (n + self.shrinkage_k)
            if n < self.min_samples:
                notes.append(f"thin window: {n} samples, pooled {solved:.3f} "
                             f"toward prior {prior:.3f}")

        decay = session_end_decay(remaining_session_minutes)
        if decay < 1.0:
            notes.append(f"session-end decay x{decay:.2f}")
        w = raw * decay

        ar_adj, ar_note = ar_farming_adjustment(w, ar)
        if ar_note:
            notes.append(ar_note)
        w += ar_adj

        floor_applied = w < self.marginal_cost_per_min
        if floor_applied:
            notes.append(f"marginal-cost floor: {w:.3f} -> {self.marginal_cost_per_min:.3f}/min")
            w = self.marginal_cost_per_min

        lo, hi = self.sanity_band
        out_of_band = not (lo <= w <= hi)
        if out_of_band:
            notes.append(f"outside sanity band: {w:.3f}/min not in [{lo}, {hi}] — check upstream")

        return RateDecision(
            w_star_per_min=w, hourly=w * 60.0, raw_w_per_min=raw,
            decay_factor=decay, ar_adjustment_per_min=ar_adj,
            out_of_band=out_of_band, floor_applied=floor_applied,
            arrival_rate_per_min=lam, sample_n=len(obs), notes=tuple(notes),
        )
