import random
from datetime import datetime, timedelta

import pytest

from blacktop.counterfactual import (
    LoggedOffer, effective_threshold, replay, sweep, weekly_report,
)

T0 = datetime(2026, 8, 10, 17, 0)


def stream(n=60, seed=2, gap=3.0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        val = rng.uniform(4, 16)
        mins = rng.uniform(8, 30)
        out.append(LoggedOffer(T0 + timedelta(minutes=gap * i), val, mins,
                               was_accepted=(val / mins) >= 0.6))
    return out


class TestReplay:
    def test_empty(self):
        r = replay([], 0.5)
        assert r.hourly == 0.0 and r.accepted == 0

    def test_accept_everything_at_zero_threshold(self):
        offers = [LoggedOffer(T0, 10.0, 20.0), LoggedOffer(T0 + timedelta(minutes=30), 12.0, 20.0)]
        r = replay(offers, 0.0)
        assert r.accepted == 2
        assert r.total_earnings == 22.0
        # wall: from first seen to completion of last = 50 min
        assert r.wall_minutes == pytest.approx(50.0)
        assert r.hourly == pytest.approx(22.0 / 50.0 * 60.0)

    def test_impossible_threshold_declines_all(self):
        r = replay(stream(), 10.0)
        assert r.accepted == 0 and r.declined > 0
        assert r.hourly == 0.0

    def test_busy_offers_are_missed_not_declined(self):
        offers = [
            LoggedOffer(T0, 10.0, 30.0),                       # accepted, busy 30 min
            LoggedOffer(T0 + timedelta(minutes=10), 50.0, 10.0),  # arrives while busy
            LoggedOffer(T0 + timedelta(minutes=40), 10.0, 10.0),  # idle again
        ]
        r = replay(offers, 0.1)
        assert r.accepted == 2
        assert r.missed_while_busy == 1

    def test_threshold_boundary_inclusive(self):
        offers = [LoggedOffer(T0, 12.0, 20.0)]   # exactly 0.6/min
        assert replay(offers, 0.6).accepted == 1
        assert replay(offers, 0.601).accepted == 0

    def test_deterministic(self):
        s = stream()
        assert replay(s, 0.5) == replay(s, 0.5)


class TestSweep:
    def test_sorted_and_complete(self):
        rs = sweep(stream(), [0.7, 0.5, 0.6])
        assert [r.threshold_per_min for r in rs] == [0.5, 0.6, 0.7]

    def test_moderate_threshold_beats_extremes(self):
        s = stream(n=120, seed=5, gap=2.0)
        rs = sweep(s, [0.0, 0.55, 5.0])
        by_t = {r.threshold_per_min: r.hourly for r in rs}
        assert by_t[0.55] > by_t[5.0]
        assert by_t[0.55] >= by_t[0.0]


class TestEffectiveThreshold:
    def test_recovers_known_threshold(self):
        s = stream(n=100, seed=6)
        t = effective_threshold(s)
        assert t is not None
        # Every decision in the synthetic log used 0.6; inferred cut must
        # classify perfectly, so it lies at/below 0.6 but above the max
        # declined rate.
        declined_max = max(o.rate_per_min for o in s if not o.was_accepted)
        accepted_min = min(o.rate_per_min for o in s if o.was_accepted)
        assert declined_max < t <= accepted_min + 1e-9

    def test_degenerate_all_accepted(self):
        s = [LoggedOffer(T0, 10, 10, was_accepted=True)] * 3
        assert effective_threshold(s) is None

    def test_degenerate_empty(self):
        assert effective_threshold([]) is None


class TestWeeklyReport:
    def test_report_shape(self):
        rep = weekly_report(stream(n=100, seed=6))
        assert "Your effective threshold was $" in rep
        assert "Simulated at $" in rep
        assert "%" in rep

    def test_report_handles_no_history(self):
        assert "Not enough" in weekly_report([])
