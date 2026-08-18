import random
from datetime import datetime, timedelta

import pytest

from blacktop.reservation import (
    ARPolicyInputs, OfferObservation, ReservationRateEngine,
    RollingOfferWindow, SANITY_BAND_PER_MIN, ar_farming_adjustment,
    completion_risk, session_end_decay, solve_w_star,
)

T0 = datetime(2026, 8, 18, 18, 0)


def obs_stream(n=20, seed=1, gap_min=1.0, lo=4.0, hi=15.0, dlo=8.0, dhi=30.0):
    rng = random.Random(seed)
    return [OfferObservation(T0 + timedelta(minutes=i * gap_min),
                             rng.uniform(lo, hi), rng.uniform(dlo, dhi))
            for i in range(n)]


class TestRollingWindow:
    def test_prefers_fresh_when_window_has_enough(self):
        w = RollingOfferWindow(window_minutes=20, min_samples=3)
        w.observe(OfferObservation(T0, 10, 20))                      # stale
        for i in range(3):
            w.observe(OfferObservation(T0 + timedelta(minutes=25 + i), 10, 20))
        snap = w.snapshot(T0 + timedelta(minutes=27))
        assert len(snap) == 3
        assert all(o.seen_at > T0 for o in snap)

    def test_reaches_back_when_window_is_thin(self):
        # A busy driver sees almost nothing inside the nominal window. Rather
        # than starve the solve, the window reaches back for min_samples.
        w = RollingOfferWindow(window_minutes=20, min_samples=4)
        for i in range(4):
            w.observe(OfferObservation(T0 + timedelta(minutes=i), 10, 20))
        w.observe(OfferObservation(T0 + timedelta(minutes=90), 10, 20))
        snap = w.snapshot(T0 + timedelta(minutes=92))
        assert len(snap) == 4

    def test_evicts_beyond_retention(self):
        w = RollingOfferWindow(window_minutes=20, min_samples=2, retain_minutes=60)
        w.observe(OfferObservation(T0, 10, 20))
        w.observe(OfferObservation(T0 + timedelta(minutes=200), 10, 20))
        assert len(w.snapshot(T0 + timedelta(minutes=200))) == 1

    def test_arrival_rate(self):
        w = RollingOfferWindow(window_minutes=30)
        for i in range(15):
            w.observe(OfferObservation(T0 + timedelta(minutes=2 * i), 10, 20))
        assert w.arrival_rate_per_min(T0 + timedelta(minutes=29)) == pytest.approx(0.5)

    def test_arrival_rate_uses_true_span_when_reaching_back(self):
        # 5 offers over 100 minutes must read as 0.05/min, not 5/30.
        w = RollingOfferWindow(window_minutes=30, min_samples=5)
        for i in range(5):
            w.observe(OfferObservation(T0 + timedelta(minutes=i * 25), 10, 20))
        lam = w.arrival_rate_per_min(T0 + timedelta(minutes=100))
        assert lam == pytest.approx(5 / 100.0)

    def test_window_bounds_validated(self):
        with pytest.raises(ValueError):
            RollingOfferWindow(window_minutes=5)
        with pytest.raises(ValueError):
            RollingOfferWindow(window_minutes=90)


class TestSolveWStar:
    def test_empty_returns_zero(self):
        assert solve_w_star([], 0.5) == 0.0
        assert solve_w_star(obs_stream(), 0.0) == 0.0

    def test_single_offer_value(self):
        # One offer type: value 12 over 20 min, arriving every 2 min.
        obs = [OfferObservation(T0 + timedelta(minutes=2 * i), 12.0, 20.0)
               for i in range(10)]
        w = solve_w_star(obs, 0.5)
        # Accept-everything rate: 12 / (1/0.5 + 20) = 0.545...
        assert w == pytest.approx(12.0 / 22.0)

    def test_selectivity_beats_accept_everything(self):
        # Half junk (rate 0.2/min), half good (rate 1.0/min), fast arrivals:
        # skipping junk must beat taking everything.
        obs = []
        for i in range(20):
            if i % 2 == 0:
                obs.append(OfferObservation(T0 + timedelta(minutes=i), 4.0, 20.0))
            else:
                obs.append(OfferObservation(T0 + timedelta(minutes=i), 15.0, 15.0))
        lam = 1.0
        w = solve_w_star(obs, lam)
        accept_all = ((4.0 + 15.0) / 2) / (1.0 / lam + 17.5)
        assert w > accept_all

    def test_indifference_fixed_point_property(self):
        obs = obs_stream(n=40, seed=3)
        lam = 1.0
        w = solve_w_star(obs, lam)
        # w* equals the maximum achievable rate; no threshold does better.
        rates = sorted({o.rate_per_min for o in obs})
        for t in rates:
            acc = [o for o in obs if o.rate_per_min >= t]
            if not acc:
                continue
            p = len(acc) / len(obs)
            r = (sum(o.value_dollars for o in acc) / len(acc)) / (
                1.0 / (lam * p) + sum(o.duration_minutes for o in acc) / len(acc))
            assert r <= w + 1e-9

    def test_higher_arrival_rate_raises_w(self):
        obs = obs_stream(n=40, seed=4)
        assert solve_w_star(obs, 1.0) > solve_w_star(obs, 0.1)


class TestSessionEndDecay:
    def test_no_planned_end(self):
        assert session_end_decay(None) == 1.0

    def test_outside_horizon(self):
        assert session_end_decay(120.0) == 1.0
        assert session_end_decay(45.0) == 1.0

    def test_linear_inside_horizon(self):
        assert session_end_decay(22.5) == pytest.approx(0.5)
        assert session_end_decay(0.0) == 0.0

    def test_monotone(self):
        vals = [session_end_decay(m) for m in (0, 10, 20, 30, 44, 45, 60)]
        assert vals == sorted(vals)


class TestARFarming:
    def test_no_adjustment_in_dense_zone(self):
        ar = ARPolicyInputs(0.40, 0.50, 3, zone_is_sparse=False)
        adj, note = ar_farming_adjustment(0.6, ar)
        assert adj == 0.0 and note is None

    def test_no_adjustment_when_above_target(self):
        ar = ARPolicyInputs(0.55, 0.50, 3, zone_is_sparse=True)
        assert ar_farming_adjustment(0.6, ar)[0] == 0.0

    def test_no_adjustment_far_from_month_end(self):
        ar = ARPolicyInputs(0.40, 0.50, 15, zone_is_sparse=True)
        assert ar_farming_adjustment(0.6, ar)[0] == 0.0

    def test_adjustment_when_farming_applies(self):
        ar = ARPolicyInputs(0.47, 0.50, 4, zone_is_sparse=True)
        adj, note = ar_farming_adjustment(0.6, ar)
        assert adj < 0.0
        assert "AR farming" in note

    def test_adjustment_capped_at_quarter(self):
        ar = ARPolicyInputs(0.10, 0.50, 1, zone_is_sparse=True)
        adj, _ = ar_farming_adjustment(0.6, ar)
        assert adj == pytest.approx(-0.15)   # 25% of 0.6

    def test_none_inputs(self):
        assert ar_farming_adjustment(0.6, None) == (0.0, None)


class TestCompletionRisk:
    def test_open_ended_session_never_risky(self):
        assert not completion_risk(90.0, None)

    def test_fits_with_grace(self):
        assert not completion_risk(50.0, 40.0, grace_minutes=20.0)

    def test_too_long(self):
        assert completion_risk(70.0, 40.0, grace_minutes=20.0)


class TestEngine:
    def test_cold_start_uses_prior(self):
        eng = ReservationRateEngine(prior_w_per_min=0.5)
        d = eng.current_rate(T0)
        assert d.w_star_per_min == 0.5
        assert d.sample_n == 0
        assert any("cold start" in n for n in d.notes)

    def test_thin_window_pools_instead_of_snapping_to_prior(self):
        # The starvation bug: at high utilization the window holds 2-3 offers
        # and a hard cold-start switch pinned w* to the constant prior forever,
        # which declines every offer a lean market can produce. Pooling must
        # move the estimate toward the evidence.
        eng = ReservationRateEngine(prior_w_per_min=0.50, min_samples=5)
        for i in range(3):
            eng.observe(OfferObservation(T0 + timedelta(minutes=i * 8), 6.0, 30.0))
        d = eng.current_rate(T0 + timedelta(minutes=20))
        assert d.w_star_per_min < 0.50
        assert any("thin window" in n for n in d.notes)

    def test_learned_prior_overrides_the_constant(self):
        eng = ReservationRateEngine(prior_w_per_min=0.50,
                                    prior_provider=lambda now: 0.22)
        d = eng.current_rate(T0)
        assert d.w_star_per_min == pytest.approx(0.22)

    def test_prior_provider_none_falls_back_to_constant(self):
        eng = ReservationRateEngine(prior_w_per_min=0.40,
                                    prior_provider=lambda now: None)
        assert eng.current_rate(T0).w_star_per_min == pytest.approx(0.40)

    def test_warm_rate_in_band_and_hourly_consistent(self):
        eng = ReservationRateEngine()
        for o in obs_stream(30, seed=7, gap_min=0.9):
            eng.observe(o)
        d = eng.current_rate(T0 + timedelta(minutes=27))
        lo, hi = SANITY_BAND_PER_MIN
        assert lo <= d.w_star_per_min <= hi
        assert d.hourly == pytest.approx(d.w_star_per_min * 60)
        assert not d.out_of_band

    def test_sanity_band_flags_but_does_not_override(self):
        # Absurdly rich stream forces w above the band. The band is a
        # diagnostic (PRD F6): it must flag the anomaly and leave w* alone.
        # Overriding it with the band edge would reinstate a fixed threshold.
        eng = ReservationRateEngine()
        for i in range(30):
            eng.observe(OfferObservation(T0 + timedelta(minutes=i * 0.5), 40.0, 10.0))
        d = eng.current_rate(T0 + timedelta(minutes=15))
        assert d.out_of_band
        assert d.w_star_per_min > SANITY_BAND_PER_MIN[1]
        assert any("outside sanity band" in n for n in d.notes)

    def test_lean_market_rate_is_not_floored_to_the_band(self):
        # A genuinely lean stream (~$0.18/min) must produce a low w*, not the
        # band floor -- otherwise the engine declines every offer available.
        eng = ReservationRateEngine()
        for i in range(30):
            eng.observe(OfferObservation(T0 + timedelta(minutes=i * 2.0), 5.4, 30.0))
        d = eng.current_rate(T0 + timedelta(minutes=58))
        assert d.w_star_per_min < SANITY_BAND_PER_MIN[0]
        assert d.out_of_band and not d.floor_applied

    def test_marginal_cost_floor_binds(self):
        eng = ReservationRateEngine(marginal_cost_per_min=0.25)
        for i in range(30):
            eng.observe(OfferObservation(T0 + timedelta(minutes=i * 2.0), 3.0, 30.0))
        d = eng.current_rate(T0 + timedelta(minutes=58))
        assert d.floor_applied
        assert d.w_star_per_min == pytest.approx(0.25)

    def test_decay_lowers_rate_near_session_end(self):
        eng = ReservationRateEngine()
        for o in obs_stream(30, seed=7, gap_min=0.9):
            eng.observe(o)
        now = T0 + timedelta(minutes=27)
        base = eng.current_rate(now)
        late = eng.current_rate(now, remaining_session_minutes=10.0)
        assert late.w_star_per_min <= base.w_star_per_min
        assert late.decay_factor == pytest.approx(10.0 / 45.0)

    def test_ar_layer_composes(self):
        eng = ReservationRateEngine()
        for o in obs_stream(30, seed=7, gap_min=0.9):
            eng.observe(o)
        now = T0 + timedelta(minutes=27)
        ar = ARPolicyInputs(0.44, 0.50, 2, zone_is_sparse=True)
        d = eng.current_rate(now, ar=ar)
        assert d.ar_adjustment_per_min < 0.0
