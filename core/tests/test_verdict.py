from datetime import datetime

import pytest

from blacktop.models import Offer, Platform, VerdictColor
from blacktop.reservation import RateDecision
from blacktop.verdict import VerdictConfig, VerdictEngine

NOW = datetime(2026, 8, 18, 18, 0)


def rate(hourly=33.0):
    per_min = hourly / 60.0
    return RateDecision(w_star_per_min=per_min, hourly=hourly, raw_w_per_min=per_min,
                        decay_factor=1.0, ar_adjustment_per_min=0.0, clamped=False,
                        arrival_rate_per_min=0.5, sample_n=20)


def offer(payout=7.75, dist=2.3, minutes=25.0, **kw):
    return Offer("o1", Platform.DOORDASH, NOW, payout,
                 stated_distance_mi=dist, stated_minutes=minutes, **kw)


class StubRoutes:
    def __init__(self, to_m, to_c):
        self.to_m, self.to_c = to_m, to_c

    def drive_to_merchant_minutes(self, offer):
        return self.to_m

    def drive_to_customer_minutes(self, offer):
        return self.to_c


class TestTimeModel:
    def test_all_providers_injected(self):
        eng = VerdictEngine(
            merchant_wait=lambda o, t: 6.0,
            dropoff_friction=lambda o: 3.0,
            return_to_density=lambda o, t: 8.0,
            route_provider=StubRoutes(4.0, 11.0))
        tb = eng.build_time_breakdown(offer(), NOW)
        assert tb.drive_to_merchant_min == 4.0
        assert tb.merchant_wait_min == 6.0
        assert tb.drive_to_customer_min == 11.0
        assert tb.dropoff_friction_min == 3.0
        assert tb.return_to_density_min == 8.0
        assert tb.total_minutes == 32.0

    def test_heuristic_fallback_from_stated_minutes(self):
        eng = VerdictEngine()
        tb = eng.build_time_breakdown(offer(minutes=20.0), NOW)
        total_drive = tb.drive_to_merchant_min + tb.drive_to_customer_min
        assert total_drive == pytest.approx(20.0 * 1.10)

    def test_heuristic_fallback_from_distance_only(self):
        eng = VerdictEngine()
        tb = eng.build_time_breakdown(offer(dist=5.0, minutes=None), NOW)
        total_drive = tb.drive_to_merchant_min + tb.drive_to_customer_min
        assert total_drive == pytest.approx(5.0 / 25.0 * 60.0 * 1.10)

    def test_heuristic_fallback_nothing_stated(self):
        eng = VerdictEngine()
        tb = eng.build_time_breakdown(offer(dist=None, minutes=None), NOW)
        assert tb.total_minutes > 0

    def test_deadhead_term_present_by_default(self):
        tb = VerdictEngine().build_time_breakdown(offer(), NOW)
        assert tb.return_to_density_min > 0    # the differentiator is never dropped


class TestValueModel:
    def test_tip_estimate_added(self):
        eng = VerdictEngine(tip_estimator=lambda o: 3.5)
        payout, tip = eng.expected_payout(offer(payout=7.0))
        assert payout == 10.5 and tip == 3.5

    def test_negative_tip_clamped(self):
        eng = VerdictEngine(tip_estimator=lambda o: -2.0)
        payout, tip = eng.expected_payout(offer(payout=7.0))
        assert payout == 7.0 and tip == 0.0

    def test_no_estimator_means_displayed_only(self):
        payout, tip = VerdictEngine().expected_payout(offer(payout=7.0))
        assert payout == 7.0 and tip == 0.0


class TestVerdicts:
    def make_engine(self, total_minutes=20.0, tip=0.0):
        share = total_minutes / 4
        return VerdictEngine(
            tip_estimator=lambda o: tip,
            merchant_wait=lambda o, t: share,
            dropoff_friction=lambda o: share,
            return_to_density=lambda o, t: 0.0,
            route_provider=StubRoutes(share, share))

    def test_green(self):
        eng = self.make_engine(total_minutes=20.0)
        # $15 over 20 min = $45/hr vs $33 threshold -> GREEN
        v = eng.evaluate(offer(payout=15.0), NOW, rate(33.0))
        assert v.color is VerdictColor.GREEN
        assert v.projected_net_hourly == pytest.approx(45.0)
        assert "Take it" in v.tts_text and "45" in v.tts_text

    def test_red(self):
        eng = self.make_engine(total_minutes=20.0)
        v = eng.evaluate(offer(payout=6.0), NOW, rate(33.0))   # $18/hr
        assert v.color is VerdictColor.RED
        assert "Skip it" in v.tts_text

    def test_amber_inside_band(self):
        eng = self.make_engine(total_minutes=20.0)
        v = eng.evaluate(offer(payout=11.0), NOW, rate(33.0))  # $33/hr exactly
        assert v.color is VerdictColor.AMBER
        assert "Close call" in v.tts_text

    def test_threshold_always_displayed(self):
        eng = self.make_engine()
        v = eng.evaluate(offer(payout=15.0), NOW, rate(33.0))
        assert v.reservation_rate_hourly == 33.0   # PRD 9.5

    def test_low_confidence_yields_manual_fallback(self):
        eng = self.make_engine()
        v = eng.evaluate(offer(payout=15.0), NOW, rate(33.0), parse_confidence=0.5)
        assert v.color is VerdictColor.MANUAL_FALLBACK
        assert not v.is_actionable
        assert v.time_breakdown is None

    def test_confidence_boundary(self):
        eng = self.make_engine()
        v = eng.evaluate(offer(payout=15.0), NOW, rate(33.0), parse_confidence=0.75)
        assert v.color is not VerdictColor.MANUAL_FALLBACK

    def test_completion_risk_forces_red(self):
        eng = self.make_engine(total_minutes=40.0)
        v = eng.evaluate(offer(payout=40.0), NOW, rate(33.0),
                         remaining_session_minutes=10.0)
        assert v.color is VerdictColor.RED
        assert "finish" in v.tts_text.lower()

    def test_tip_moves_verdict(self):
        eng_no_tip = self.make_engine(total_minutes=20.0, tip=0.0)
        eng_tip = self.make_engine(total_minutes=20.0, tip=6.0)
        o = offer(payout=9.0)
        assert eng_no_tip.evaluate(o, NOW, rate(33.0)).color is VerdictColor.RED
        assert eng_tip.evaluate(o, NOW, rate(33.0)).color is VerdictColor.GREEN

    def test_verdict_is_pure_value_object(self):
        v = self.make_engine().evaluate(offer(), NOW, rate())
        # Advisory-only invariant: the verdict carries no callables/actions.
        assert not any(callable(getattr(v, f)) for f in
                       ("color", "tts_text", "projected_net_hourly"))

    def test_custom_config_band(self):
        eng = VerdictEngine(
            merchant_wait=lambda o, t: 5.0, dropoff_friction=lambda o: 5.0,
            return_to_density=lambda o, t: 0.0, route_provider=StubRoutes(5.0, 5.0),
            config=VerdictConfig(amber_band=0.0))
        v = eng.evaluate(offer(payout=11.01), NOW, rate(33.0))
        assert v.color is VerdictColor.GREEN
