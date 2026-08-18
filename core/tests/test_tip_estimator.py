from datetime import datetime

import pytest

from blacktop.models import Offer, Platform
from blacktop.tip_estimator import (
    DEFAULT_MARKET_PRIOR_TIP, DisplayCapDetector, TipEstimator,
)

NOW = datetime(2026, 8, 18, 18, 0)


def mk(payout, cap=False, mid="m1", name=""):
    return Offer("x", Platform.DOORDASH, NOW, payout,
                 merchant_id=mid, merchant_name=name, hit_display_cap=cap)


class TestCapDetector:
    def test_no_cap_without_repeats(self):
        d = DisplayCapDetector(min_repeats=5)
        for v in (5.0, 6.25, 7.0, 9.75, 8.5):
            d.observe(v)
        assert d.cap_value is None

    def test_detects_modal_top_value(self):
        d = DisplayCapDetector(min_repeats=5)
        for _ in range(8):
            d.observe(9.75)
        for v in (4.0, 5.5, 6.0, 7.25, 8.0):
            d.observe(v)
        assert d.cap_value == 9.75
        assert d.is_at_cap(9.75)
        assert not d.is_at_cap(9.74)

    def test_recurring_mid_value_is_not_cap_when_mass_above(self):
        d = DisplayCapDetector(min_repeats=5)
        for _ in range(6):
            d.observe(6.0)          # recurring, but not the ceiling
        for v in (7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0):
            d.observe(v)            # >5% of mass above 6.0
        assert d.cap_value is None

    def test_cents_rounding(self):
        d = DisplayCapDetector(min_repeats=3)
        for _ in range(3):
            d.observe(9.754999)
        assert d.cap_value == pytest.approx(9.75)


class TestPartialPooling:
    def test_cold_start_returns_market_prior(self):
        te = TipEstimator()
        assert te.expected_tip(mk(6.0)) == pytest.approx(DEFAULT_MARKET_PRIOR_TIP)

    def test_estimate_moves_toward_observed_mean(self):
        te = TipEstimator()
        for _ in range(30):
            te.observe_delivery(mk(6.0, mid="mA"), 6.0 + 5.0)
        est = te.expected_tip(mk(6.0, mid="mA"))
        assert DEFAULT_MARKET_PRIOR_TIP < est < 5.0
        assert est > 4.0

    def test_merchant_level_shrinks_to_market(self):
        te = TipEstimator()
        # Market data: many merchants tipping $2.
        for i in range(50):
            te.observe_delivery(mk(6.0, mid=f"m{i}"), 8.0)
        # A single $10-tip observation for a new merchant should NOT dominate.
        te.observe_delivery(mk(6.0, mid="whale"), 16.0)
        est = te.expected_tip(mk(6.0, mid="whale"))
        assert est < 4.0

    def test_category_layer_between_market_and_merchant(self):
        te = TipEstimator()
        for i in range(40):
            te.observe_delivery(mk(6.0, mid=f"a{i}"), 6.0 + 2.0, merchant_category="fast_food")
        for i in range(40):
            te.observe_delivery(mk(6.0, mid=f"b{i}"), 6.0 + 6.0, merchant_category="steakhouse")
        fast = te.expected_tip(mk(6.0, mid="new1"), "fast_food")
        steak = te.expected_tip(mk(6.0, mid="new2"), "steakhouse")
        assert steak > fast

    def test_unknown_merchant_key_uses_category_path(self):
        te = TipEstimator()
        est = te.expected_tip(Offer("x", Platform.DOORDASH, NOW, 6.0))
        assert est == pytest.approx(DEFAULT_MARKET_PRIOR_TIP)


class TestCapSignal:
    def build(self):
        te = TipEstimator()
        for _ in range(10):
            te.observe_offer(mk(9.75))            # establish the cap
        for _ in range(20):
            te.observe_delivery(mk(6.0, mid="m"), 6.0 + 3.0)     # $3 tips uncapped
        for _ in range(8):
            te.observe_delivery(mk(9.75, cap=True), 9.75 + 9.0)  # $9 excess capped
        return te

    def test_capped_offer_gets_boost(self):
        te = self.build()
        uncapped = te.expected_tip(mk(6.0, mid="m"))
        capped = te.expected_tip(mk(9.75))
        assert capped > uncapped

    def test_capped_observations_kept_out_of_base_mean(self):
        te = self.build()
        # Base estimate stays near $3, unpolluted by the capped $9s.
        assert te.expected_tip(mk(6.0, mid="m")) < 4.0

    def test_explicit_flag_equivalent_to_detected_cap(self):
        te = self.build()
        assert te.expected_tip(mk(5.0, cap=True)) == pytest.approx(
            te.expected_tip(mk(9.75)), abs=0.75)

    def test_cap_estimate_at_least_base(self):
        te = TipEstimator()
        for _ in range(20):
            te.observe_delivery(mk(6.0, mid="m"), 6.0 + 5.0)
        assert te.expected_tip(mk(6.0, mid="m", cap=True)) >= te.expected_tip(mk(6.0, mid="m"))

    def test_label_count(self):
        te = self.build()
        assert te.label_count == 28
