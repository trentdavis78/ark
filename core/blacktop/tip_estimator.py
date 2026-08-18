"""F2 — Hidden-Tip Estimator (reference / cold-start path).

Predicts the concealed portion of the payout. Labels arrive for free: after
completion the actual payout is compared with what the card displayed
(``Delivery.actual_tip_delta``).

This module implements the PRD's cold-start design — hierarchical partial
pooling so the estimate is useful at delivery 20, not delivery 500:

    market prior  ->  merchant-category posterior  ->  merchant posterior

Each level shrinks toward its parent with strength ``k`` (pseudo-count):
    est(level) = (n * mean_obs + k * est(parent)) / (n + k)

On top of the pooled base estimate, two adjustments from the PRD:

* ``hit_display_cap`` — the highest-signal feature. When the displayed amount
  sits at the market's detected cap, the true payout is systematically higher;
  we learn the average cap excess from capped observations and add it.
* Peak Pay is already in the displayed amount, so it does NOT increase the
  hidden-tip estimate (it slightly reduces the tip share empirically; we stay
  neutral in the reference model).

The trained-model path (LightGBM -> ONNX) lives in ``ml/``; both share the
feature list via ``ml/features.py``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .models import Offer

DEFAULT_MARKET_PRIOR_TIP = 3.00   # NJ suburban prior, dollars
DEFAULT_SHRINKAGE_K = 10.0


@dataclass
class _RunningMean:
    n: int = 0
    total: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        self.total += x

    @property
    def mean(self) -> float:
        return self.total / self.n if self.n else 0.0


class DisplayCapDetector:
    """Detects the market-specific displayed-payout cap ('total may be
    higher') by counting: if offers repeatedly land on the identical dollar
    figure at the top of the distribution, that's the ceiling.

    A value qualifies as the cap when it (a) recurs at least ``min_repeats``
    times, and (b) is the maximum among recurring high values (nothing
    recurring sits above it)."""

    def __init__(self, min_repeats: int = 5) -> None:
        self.min_repeats = min_repeats
        self.counts: Counter[float] = Counter()

    def observe(self, displayed_payout: float) -> None:
        self.counts[round(displayed_payout, 2)] += 1

    @property
    def cap_value(self) -> float | None:
        candidates = [v for v, c in self.counts.items() if c >= self.min_repeats]
        if not candidates:
            return None
        cap = max(candidates)
        # Guard: the cap must actually be at the top of the distribution —
        # nothing observed meaningfully above it more than once.
        above = sum(c for v, c in self.counts.items() if v > cap + 0.005)
        total = sum(self.counts.values())
        if above > 0.05 * total:
            return None
        return cap

    def is_at_cap(self, displayed_payout: float) -> bool:
        cap = self.cap_value
        return cap is not None and abs(displayed_payout - cap) < 0.005


class TipEstimator:
    """Hierarchical partial-pooling estimator with cap adjustment."""

    def __init__(self,
                 market_prior_tip: float = DEFAULT_MARKET_PRIOR_TIP,
                 shrinkage_k: float = DEFAULT_SHRINKAGE_K,
                 cap_detector: DisplayCapDetector | None = None) -> None:
        self.market_prior = market_prior_tip
        self.k = shrinkage_k
        self.cap_detector = cap_detector or DisplayCapDetector()
        self._market = _RunningMean()
        self._by_category: dict[str, _RunningMean] = defaultdict(_RunningMean)
        self._by_merchant: dict[str, _RunningMean] = defaultdict(_RunningMean)
        self._cap_excess = _RunningMean()   # (actual - displayed) on capped offers

    # ------------------------------------------------------------ learning
    def observe_offer(self, offer: Offer) -> None:
        """Every seen offer (accept or decline) feeds the cap detector."""
        self.cap_detector.observe(offer.displayed_payout)

    def observe_delivery(self, offer: Offer, actual_payout: float,
                         merchant_category: str = "") -> None:
        """A completed delivery: the free label."""
        tip_delta = max(0.0, actual_payout - offer.displayed_payout)
        was_capped = offer.hit_display_cap or self.cap_detector.is_at_cap(offer.displayed_payout)
        if was_capped:
            # Capped observations bias the plain tip mean upward; learn the
            # cap excess separately and keep the base distribution clean.
            self._cap_excess.add(tip_delta)
            return
        self._market.add(tip_delta)
        if merchant_category:
            self._by_category[merchant_category].add(tip_delta)
        key = offer.merchant_id or offer.merchant_name
        if key:
            self._by_merchant[key].add(tip_delta)

    # ----------------------------------------------------------- inference
    def _pooled(self, stats: _RunningMean, parent_est: float) -> float:
        return (stats.n * stats.mean + self.k * parent_est) / (stats.n + self.k)

    def expected_tip(self, offer: Offer, merchant_category: str = "") -> float:
        market_est = self._pooled(self._market, self.market_prior)
        cat_est = (self._pooled(self._by_category[merchant_category], market_est)
                   if merchant_category else market_est)
        key = offer.merchant_id or offer.merchant_name
        base = self._pooled(self._by_merchant[key], cat_est) if key else cat_est

        at_cap = offer.hit_display_cap or self.cap_detector.is_at_cap(offer.displayed_payout)
        if at_cap:
            # Strong positive predictor (PRD F2). Use learned cap excess when
            # we have it, floored at the base estimate.
            excess = self._pooled(self._cap_excess, base * 1.5)
            return max(base, excess)
        return base

    @property
    def label_count(self) -> int:
        return self._market.n + self._cap_excess.n
