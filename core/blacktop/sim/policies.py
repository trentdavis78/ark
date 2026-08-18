"""The two policies the simulator puts head to head.

``DollarPerMilePolicy`` is the incumbent: the static "$2 per mile" heuristic
that PRD 1 identifies as the thing drivers actually do. It reads the card and
nothing else.

``BlacktopPolicy`` is the product: F2 tip estimate, F4 merchant wait, learned
deadhead, the five-term F1 time model, and the F6 dynamic reservation rate.
It learns only from the driver's own observations (I5) and every decision it
makes is a recommendation a human would tap (I3/I4).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from ..merchant_oracle import MerchantWaitOracle
from ..models import DestinationClass, Offer, WeatherBucket
from ..reservation import ARPolicyInputs, OfferObservation, ReservationRateEngine
from ..tip_estimator import TipEstimator
from ..verdict import VerdictConfig, VerdictEngine
from ..zone_map import ZoneMap
from .world import SimOffer


class Policy(Protocol):
    name: str

    def decide(self, so: SimOffer, remaining_minutes: float) -> bool: ...
    def observe_seen(self, so: SimOffer) -> None: ...
    def observe_completed(self, so: SimOffer) -> None: ...


# --------------------------------------------------------------------------- #
# Incumbent
# --------------------------------------------------------------------------- #

@dataclass
class DollarPerMilePolicy:
    """Accept iff displayed payout / stated distance >= threshold.

    Both inputs are the card's own numbers, which is precisely why this fails:
    the numerator hides the tip and the denominator is straight-line. It is
    also blind to merchant wait and to deadhead, so it cannot distinguish a
    2-mile dense run from a 9-mile fringe run that ends 19 minutes from the
    next offer.
    """
    threshold_per_mile: float = 2.00
    name: str = "baseline $/mi"

    def decide(self, so: SimOffer, remaining_minutes: float) -> bool:
        d = so.offer.stated_distance_mi or 0.0
        if d <= 0:
            return False
        return so.offer.displayed_payout / d >= self.threshold_per_mile

    def observe_seen(self, so: SimOffer) -> None:      # learns nothing
        return None

    def observe_completed(self, so: SimOffer) -> None:
        return None


# --------------------------------------------------------------------------- #
# BLACKTOP
# --------------------------------------------------------------------------- #

@dataclass
class _RunningMean:
    n: int = 0
    mean: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        self.mean += (x - self.mean) / self.n


@dataclass
class BlacktopPolicy:
    """The full stack. Providers are wired to the learned models, so the
    quality of the decision improves monotonically with logged history."""

    weather: WeatherBucket = WeatherBucket.CLEAR
    ar_policy: ARPolicyInputs | None = None
    name: str = "BLACKTOP"

    # True marginal cost of a driving minute. The reservation rate is never
    # allowed below this: an offer that does not clear the cost of driving it
    # is a loss no matter how slow the market is.
    marginal_cost_per_min: float = 0.0

    tips: TipEstimator = field(default_factory=TipEstimator)
    waits: MerchantWaitOracle = field(default_factory=MerchantWaitOracle)
    zones: ZoneMap = field(default_factory=ZoneMap)
    rates: ReservationRateEngine = field(init=False)

    # Learned deadhead per dropoff hex, and friction per destination class.
    # Both are the driver's own realized observations (I5). Before a hex or a
    # class has history, F1's documented defaults apply.
    _deadhead: dict[str, _RunningMean] = field(default_factory=lambda: defaultdict(_RunningMean))
    _friction: dict[DestinationClass, _RunningMean] = field(
        default_factory=lambda: defaultdict(_RunningMean))

    _engine: VerdictEngine = field(init=False)
    _category: dict[str, str] = field(default_factory=dict)
    _global_rate: _RunningMean = field(default_factory=_RunningMean)
    _current_hex: str = ""
    _last_rate_hourly: float = 0.0
    _amber: int = 0

    def __post_init__(self) -> None:
        self.rates = ReservationRateEngine(
            marginal_cost_per_min=self.marginal_cost_per_min,
            prior_provider=self._prior_rate)
        self._engine = VerdictEngine(
            tip_estimator=self._tip_for,
            merchant_wait=self._wait_for,
            dropoff_friction=self._friction_for,
            return_to_density=self._deadhead_for,
            route_provider=None,      # no routing in-sim: heuristic fallback
            config=VerdictConfig(),
        )

    # ------------------------------------------------------------ providers
    def _tip_for(self, offer: Offer) -> float:
        return self.tips.expected_tip(offer, self._category.get(offer.merchant_id or "", ""))

    def _wait_for(self, offer: Offer, now: datetime) -> float:
        return self.waits.wait_estimate(offer.merchant_id or "", now).p50

    def _friction_for(self, offer: Offer) -> float:
        rm = self._friction.get(offer.dropoff_class)
        return rm.mean if rm and rm.n >= 3 else self._engine.config.default_dropoff_friction_min

    def _deadhead_for(self, offer: Offer, now: datetime) -> float:
        rm = self._deadhead.get(offer.dropoff_hex or "")
        return rm.mean if rm and rm.n >= 3 else self._engine.config.default_return_to_density_min

    def _context(self, so: SimOffer) -> str:
        """F6 context key: hex cluster x weather. Threshold estimation must not
        pool a dense corridor with a sparse one."""
        return f"{so.offer.dropoff_hex or ''}|{self.weather.value}"

    def _prior_rate(self, now: datetime) -> float | None:
        """Context prior for F6: what offers in this hex, at this hour, in this
        weather have historically been worth per minute. Hierarchical, exactly
        as F2 does cold start — zone posterior first, driver-wide mean next,
        the module constant only if neither exists yet."""
        st = self.zones.stats(self._current_hex, now, self.weather)
        if st is not None and st.sample_n >= 5 and st.value_mean_per_min > 0:
            return st.value_mean_per_min
        if self._global_rate.n >= 5:
            return self._global_rate.mean
        return None

    # -------------------------------------------------------------- decide
    def decide(self, so: SimOffer, remaining_minutes: float) -> bool:
        now = so.offer.seen_at
        rate = self.rates.current_rate(now, remaining_session_minutes=remaining_minutes,
                                       ar=self.ar_policy, context=self._context(so))
        self._last_rate_hourly = rate.hourly
        v = self._engine.evaluate(so.offer, now, rate,
                                  parse_confidence=1.0,
                                  remaining_session_minutes=remaining_minutes)
        if not v.is_actionable:
            return False       # I6: a card we couldn't read is the driver's call
        if v.color.value == "amber":
            self._amber += 1
        # The color is the display; the decision is the comparison behind it.
        return v.projected_net_hourly >= v.reservation_rate_hourly

    # -------------------------------------------------------------- learn
    def observe_seen(self, so: SimOffer) -> None:
        """Every offer seen is an observation — declines included. This is
        what feeds the reservation rate and the zone map (PRD F3/F6)."""
        offer = so.offer
        self._category[offer.merchant_id or ""] = so.merchant.category
        self.tips.observe_offer(offer)

        now = offer.seen_at
        tb = self._engine.build_time_breakdown(offer, now)
        payout, _ = self._engine.expected_payout(offer)
        minutes = max(tb.total_minutes, 1e-9)
        self.rates.observe(OfferObservation(now, payout, minutes), context=self._context(so))
        self._current_hex = offer.dropoff_hex or ""
        self._global_rate.add(payout / minutes)
        self.zones.record_offer(self._current_hex, now, self.weather, payout / minutes)

    def observe_completed(self, so: SimOffer) -> None:
        """A completed delivery: the free label for F2 and the dwell sample
        for F4 (PRD F2, F4)."""
        t = so.truth
        offer = so.offer
        # The driver is unavailable for the whole cycle; F6 needs this to
        # measure arrival rate per minute available rather than per minute
        # elapsed.
        self.rates.note_unavailable(offer.seen_at,
                                    offer.seen_at + timedelta(minutes=t.total_minutes),
                                    context=self._context(so))
        self.tips.observe_delivery(offer, t.true_total_payout, so.merchant.category)
        arrived = offer.seen_at
        self.waits.observe_dwell(offer.merchant_id or "", arrived,
                                 arrived + timedelta(minutes=t.merchant_wait_min))
        self._deadhead[offer.dropoff_hex or ""].add(t.deadhead_min)
        self._friction[offer.dropoff_class].add(t.dropoff_friction_min)

    def record_presence(self, hex_id: str, when: datetime, hours: float) -> None:
        self.zones.record_presence(hex_id, when, self.weather, hours)
