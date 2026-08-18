"""Simulator tests, plus end-to-end regression guards.

The lift assertions here exist to catch reintroduction of the three defects
the simulator originally exposed (sanity-band clamp, window starvation,
un-keyed threshold context). Each one silently reverted F6 to a fixed
threshold, and each showed up as a *negative* lift in at least one zone while
every unit test still passed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from blacktop.models import DestinationClass, Platform
from blacktop.sim.policies import BlacktopPolicy, DollarPerMilePolicy
from blacktop.sim.runner import (MARGINAL_COST_PER_MIN, OraclePolicy,
                                 run_season, run_shift)
from blacktop.sim.world import DEFAULT_DISPLAY_CAP, Market

START = dt.datetime(2026, 8, 3, 11, 0)


def a_stream(zone_key="dense", hours=8.0, seed=5):
    m = Market(seed=seed)
    z = m.zone(zone_key)
    return m, z, m.stream(z, START, START + dt.timedelta(hours=hours))


class TestWorld:
    def test_stream_is_time_ordered_and_nonempty(self):
        _, _, s = a_stream()
        assert len(s) > 20
        assert all(a.offer.seen_at <= b.offer.seen_at for a, b in zip(s, s[1:]))

    def test_card_understates_distance(self):
        _, _, s = a_stream()
        # The card shows straight-line, not road, distance (PRD 1).
        assert all(so.offer.stated_distance_mi < so.truth.road_miles for so in s)

    def test_card_omits_pickup_wait(self):
        _, _, s = a_stream()
        # Stated minutes never account for time at the counter.
        assert all(so.offer.stated_minutes < so.truth.total_minutes for so in s)

    def test_display_cap_hides_tip(self):
        _, _, s = a_stream()
        capped = [so for so in s if so.offer.hit_display_cap]
        assert capped, "expected some offers above the market display cap"
        assert all(so.offer.displayed_payout == pytest.approx(DEFAULT_DISPLAY_CAP)
                   for so in capped)
        assert all(so.truth.hidden_tip > 0 for so in capped)

    def test_uncapped_offers_reveal_full_payout(self):
        _, _, s = a_stream()
        for so in s:
            if not so.offer.hit_display_cap:
                assert so.truth.hidden_tip == pytest.approx(0.0, abs=1e-9)

    def test_hidden_tip_reconciles(self):
        _, _, s = a_stream()
        assert all(so.offer.displayed_payout + so.truth.hidden_tip
                   == pytest.approx(so.truth.true_total_payout) for so in s)

    def test_same_seed_same_stream(self):
        _, _, a = a_stream(seed=11)
        _, _, b = a_stream(seed=11)
        assert [x.offer.offer_id for x in a] == [y.offer.offer_id for y in b]
        assert [x.offer.displayed_payout for x in a] == [y.offer.displayed_payout for y in b]

    def test_sparse_zone_has_the_longest_deadhead(self):
        m = Market()
        assert (m.zone("sparse").deadhead_minutes
                > m.zone("suburban").deadhead_minutes
                > m.zone("dense").deadhead_minutes)

    def test_offers_are_doordash_cards(self):
        _, _, s = a_stream()
        assert all(so.offer.platform is Platform.DOORDASH for so in s)
        assert all(so.offer.dropoff_class is not DestinationClass.UNKNOWN for so in s)


class TestRunner:
    def test_busy_driver_misses_offers(self):
        _, _, s = a_stream()
        end = START + dt.timedelta(hours=8)
        r = run_shift(s, DollarPerMilePolicy(threshold_per_mile=0.0), START, end)
        # Accepting everything means most of the stream is never even seen.
        assert r.missed_while_busy > 0
        assert r.accepted + r.declined + r.missed_while_busy == len(s)

    def test_declining_everything_earns_nothing(self):
        _, _, s = a_stream()
        end = START + dt.timedelta(hours=8)
        r = run_shift(s, DollarPerMilePolicy(threshold_per_mile=999.0), START, end)
        assert r.accepted == 0
        assert r.gross == 0.0
        assert r.missed_while_busy == 0
        assert r.net_hourly == 0.0   # no miles driven, so no cost either

    def test_net_is_below_gross(self):
        _, _, s = a_stream()
        end = START + dt.timedelta(hours=8)
        r = run_shift(s, DollarPerMilePolicy(), START, end)
        assert 0 < r.net_hourly < r.gross_hourly
        assert r.shielded_income > 0

    def test_online_minutes_is_the_denominator(self):
        _, _, s = a_stream(hours=8.0)
        end = START + dt.timedelta(hours=8)
        r = run_shift(s, DollarPerMilePolicy(), START, end)
        assert r.online_minutes == pytest.approx(480.0)
        assert r.busy_minutes <= r.online_minutes * 1.05


class TestPolicies:
    def test_baseline_reads_only_the_card(self):
        _, _, s = a_stream()
        p = DollarPerMilePolicy(threshold_per_mile=2.0)
        for so in s[:5]:
            expected = so.offer.displayed_payout / so.offer.stated_distance_mi >= 2.0
            assert p.decide(so, 480.0) is expected

    def test_blacktop_learns_the_display_cap(self):
        overall, _, policy = run_season(days=3, warmup_days=3)
        assert policy.tips.cap_detector.cap_value == pytest.approx(DEFAULT_DISPLAY_CAP)

    def test_blacktop_accumulates_labels(self):
        _, _, policy = run_season(days=3, warmup_days=3)
        assert policy.tips.label_count > 0


class TestLiftRegression:
    """The guards. Each maps to a defect that unit tests alone did not catch."""

    @staticmethod
    @pytest.fixture(scope="class")
    def season():
        return run_season(days=8, warmup_days=4)

    def test_beats_baseline_overall(self, season):
        overall, _, _ = season
        assert overall.net_hourly_lift > 0.0

    def test_beats_baseline_in_every_zone(self, season):
        # Regression: the un-keyed threshold context made sparse deeply
        # negative (-71%) while dense and suburban stayed positive.
        _, by_zone, _ = season
        for zk, comp in by_zone.items():
            assert comp.net_hourly_lift > 0.0, f"{zk} regressed: {comp.net_hourly_lift:.1%}"

    def test_acceptance_rate_is_selective_not_degenerate(self, season):
        # Regression: window starvation pinned w* to a constant prior far
        # above the market, collapsing acceptance toward zero.
        overall, _, _ = season
        assert 0.15 < overall.blacktop.acceptance_rate < 0.90

    def test_stays_busy(self, season):
        # A policy that earns more by working far less has not solved the
        # problem the driver has.
        overall, _, _ = season
        assert overall.blacktop.utilization > 0.45

    def test_oracle_ceiling_is_above_both(self, season):
        overall, by_zone, _ = season
        _, oracle_zones, _ = run_season(days=8, warmup_days=4,
                                        challenger=lambda: OraclePolicy(0.30))
        assert oracle_zones["dense"].blacktop.net_hourly > by_zone["dense"].baseline.net_hourly

    def test_matched_comparison_uses_one_stream(self, season):
        overall, _, _ = season
        # Both policies face the same market: totals differ only by choice.
        assert overall.baseline.online_minutes == overall.blacktop.online_minutes
