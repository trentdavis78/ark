from datetime import datetime, timedelta

import pytest

from blacktop.merchant_oracle import MerchantWaitOracle, WaitEstimate, _quantile

BASE = datetime(2026, 8, 18, 18, 0)   # Tuesday dinner


def feed(oracle, mid, minutes_list, at=BASE):
    for m in minutes_list:
        oracle.observe_dwell(mid, at, at + timedelta(minutes=m))


class TestQuantile:
    def test_single(self):
        assert _quantile([5.0], 0.9) == 5.0

    def test_median_interpolation(self):
        assert _quantile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)

    def test_p90(self):
        xs = sorted(float(i) for i in range(1, 11))
        assert _quantile(xs, 0.9) == pytest.approx(9.1)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _quantile([], 0.5)


class TestObserve:
    def test_negative_dwell_rejected(self):
        o = MerchantWaitOracle()
        with pytest.raises(ValueError):
            o.observe_dwell("m", BASE, BASE - timedelta(minutes=1))

    def test_absurd_dwell_clipped(self):
        o = MerchantWaitOracle(min_hour_samples=1)
        o.observe_dwell("m", BASE, BASE + timedelta(minutes=300))
        assert o.wait_estimate("m", BASE).p50 == 60.0


class TestEstimates:
    def test_default_when_unknown(self):
        o = MerchantWaitOracle(default_wait_min=5.0)
        est = o.wait_estimate("nowhere", BASE)
        assert est == WaitEstimate(5.0, 10.0, 0, "default")

    def test_hour_bucket_preferred(self):
        o = MerchantWaitOracle()
        feed(o, "m", [10, 11, 12, 13], at=BASE)                     # dinner hour
        feed(o, "m", [2, 2, 2, 2], at=BASE.replace(hour=10))        # morning
        est = o.wait_estimate("m", BASE)
        assert est.source == "hour"
        assert est.p50 == pytest.approx(11.5)

    def test_location_fallback_when_hour_sparse(self):
        o = MerchantWaitOracle(min_hour_samples=4, min_location_samples=3)
        feed(o, "m", [8, 9, 10], at=BASE.replace(hour=12))
        est = o.wait_estimate("m", BASE)   # no dinner samples
        assert est.source == "location"
        assert est.sample_n == 3

    def test_p90_at_least_p50(self):
        o = MerchantWaitOracle()
        feed(o, "m", [3, 5, 8, 12, 20])
        est = o.wait_estimate("m", BASE)
        assert est.p90 >= est.p50

    def test_per_location_not_per_brand(self):
        o = MerchantWaitOracle()
        feed(o, "chipotle-rt10", [15, 16, 17, 18])
        feed(o, "chipotle-morristown", [3, 3, 4, 4])
        assert o.wait_estimate("chipotle-rt10", BASE).p50 > \
            o.wait_estimate("chipotle-morristown", BASE).p50


class TestChronicOffender:
    def test_flagged(self):
        o = MerchantWaitOracle()
        feed(o, "slow", [11, 12, 13, 14])
        assert o.chronic_offender("slow", BASE)
        text = o.flag_text("slow", BASE)
        assert "min at this hour" in text and "P90" in text

    def test_fast_location_not_flagged(self):
        o = MerchantWaitOracle()
        feed(o, "fast", [2, 3, 3, 4])
        assert not o.chronic_offender("fast", BASE)
        assert o.flag_text("fast", BASE) is None

    def test_unknown_not_flagged_despite_default(self):
        o = MerchantWaitOracle(default_wait_min=30.0)
        assert not o.chronic_offender("unknown", BASE)


class TestArrivalTiming:
    def test_delay_tracks_predicted_wait(self):
        o = MerchantWaitOracle()
        feed(o, "m", [12, 12, 12, 12])
        delay = o.arrival_delay_advice("m", BASE, drive_minutes=5.0)
        assert delay == pytest.approx(10.0)   # p50 - 2 min counter buffer

    def test_bounded_by_pickup_deadline(self):
        """PRD F4: never risk a lateness violation."""
        o = MerchantWaitOracle()
        feed(o, "m", [12, 12, 12, 12])
        delay = o.arrival_delay_advice("m", BASE, drive_minutes=5.0,
                                       pickup_deadline_minutes=10.0)
        assert delay == pytest.approx(3.0)    # 10 - 5 drive - 2 buffer

    def test_never_negative(self):
        o = MerchantWaitOracle()
        feed(o, "m", [1, 1, 1, 1])
        assert o.arrival_delay_advice("m", BASE, drive_minutes=20.0,
                                      pickup_deadline_minutes=5.0) == 0.0
