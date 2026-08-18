from datetime import date, datetime

import pytest

from blacktop.mileage_tax import (
    QUARTERLY_DUE_DATES_2026, RATE_2026_H1, RATE_2026_H2, SessionMileageLog,
    _bracket_tax, estimate_taxes, mileage_deduction, mileage_rate,
    quarterly_schedule, self_employment_tax,
)
from blacktop.models import TaxParams, VehicleCostParams


class TestSplitRate:
    def test_h1_rate(self):
        assert mileage_rate(date(2026, 1, 1)) == RATE_2026_H1
        assert mileage_rate(date(2026, 6, 30)) == RATE_2026_H1

    def test_h2_rate(self):
        assert mileage_rate(date(2026, 7, 1)) == RATE_2026_H2
        assert mileage_rate(date(2026, 12, 31)) == RATE_2026_H2

    def test_values_match_prd(self):
        assert RATE_2026_H1 == 0.725
        assert RATE_2026_H2 == 0.76

    def test_other_years_refused(self):
        with pytest.raises(ValueError):
            mileage_rate(date(2025, 12, 31))
        with pytest.raises(ValueError):
            mileage_rate(date(2027, 1, 1))

    def test_deduction_spans_the_split(self):
        ded = mileage_deduction({date(2026, 6, 30): 100.0, date(2026, 7, 1): 100.0})
        assert ded == pytest.approx(72.50 + 76.00)

    def test_single_annual_rate_would_be_wrong(self):
        """The PRD's explicit trap: 200 mi split across the boundary must NOT
        equal 200 mi at either single rate."""
        ded = mileage_deduction({date(2026, 6, 30): 100.0, date(2026, 7, 1): 100.0})
        assert ded != pytest.approx(200 * RATE_2026_H1)
        assert ded != pytest.approx(200 * RATE_2026_H2)

    def test_negative_miles_rejected(self):
        with pytest.raises(ValueError):
            mileage_deduction({date(2026, 3, 1): -5.0})


class TestBracketTax:
    BRACKETS = ((10_000.0, 0.10), (30_000.0, 0.20), (float("inf"), 0.30))

    def test_zero_and_negative(self):
        assert _bracket_tax(0, self.BRACKETS) == 0.0
        assert _bracket_tax(-100, self.BRACKETS) == 0.0

    def test_first_bracket_only(self):
        assert _bracket_tax(5_000, self.BRACKETS) == pytest.approx(500.0)

    def test_spans_brackets(self):
        # 10k*0.1 + 20k*0.2 + 10k*0.3 = 1000 + 4000 + 3000
        assert _bracket_tax(40_000, self.BRACKETS) == pytest.approx(8_000.0)

    def test_boundary_exact(self):
        assert _bracket_tax(10_000, self.BRACKETS) == pytest.approx(1_000.0)


class TestSETax:
    def test_rate_and_factor(self):
        p = TaxParams()
        tax = self_employment_tax(10_000, p)
        assert tax == pytest.approx(10_000 * 0.9235 * 0.153)

    def test_zero_profit(self):
        assert self_employment_tax(0.0) == 0.0
        assert self_employment_tax(-500.0) == 0.0

    def test_ss_cap_applies(self):
        p = TaxParams()
        big = 400_000.0
        tax = self_employment_tax(big, p)
        earnings = big * p.se_net_earnings_factor
        expected = p.ss_wage_base * p.ss_rate + earnings * p.medicare_rate
        assert tax == pytest.approx(expected)
        assert tax < earnings * p.se_tax_rate   # capped below the naive 15.3%


class TestEstimateTaxes:
    def test_typical_driver_year(self):
        est = estimate_taxes(52_000, 14_000)
        assert est.net_profit == 38_000
        assert est.se_tax == pytest.approx(38_000 * 0.9235 * 0.153, abs=1.0)
        assert est.federal_income_tax > 0
        assert est.nj_income_tax > 0
        assert est.total_tax == pytest.approx(
            est.se_tax + est.federal_income_tax + est.nj_income_tax)
        assert est.quarterly_payment == pytest.approx(est.total_tax / 4, abs=0.01)

    def test_mileage_deduction_reduces_everything(self):
        no_ded = estimate_taxes(52_000, 0)
        with_ded = estimate_taxes(52_000, 14_000)
        assert with_ded.se_tax < no_ded.se_tax
        assert with_ded.federal_income_tax < no_ded.federal_income_tax
        assert with_ded.nj_income_tax < no_ded.nj_income_tax

    def test_half_se_deducted_federally_not_nj(self):
        est = estimate_taxes(52_000, 14_000)
        p = TaxParams()
        assert est.federal_taxable_income == pytest.approx(
            est.net_profit - est.se_tax / 2 - p.federal_standard_deduction, abs=1.0)
        assert est.nj_taxable_income == est.net_profit

    def test_nj_filing_threshold(self):
        est = estimate_taxes(9_500, 0)
        assert est.nj_income_tax == 0.0

    def test_deduction_larger_than_gross(self):
        est = estimate_taxes(5_000, 8_000)
        assert est.net_profit == 0.0
        assert est.total_tax == 0.0

    def test_other_expenses_reduce_net(self):
        a = estimate_taxes(52_000, 14_000, other_expenses=0)
        b = estimate_taxes(52_000, 14_000, other_expenses=2_000)
        assert b.net_profit == a.net_profit - 2_000

    def test_quarterly_schedule_dates(self):
        est = estimate_taxes(52_000, 14_000)
        sched = quarterly_schedule(est)
        assert [d for d, _ in sched] == list(QUARTERLY_DUE_DATES_2026)
        assert all(amt == est.quarterly_payment for _, amt in sched)
        assert QUARTERLY_DUE_DATES_2026[0] == date(2026, 4, 15)
        assert QUARTERLY_DUE_DATES_2026[3] == date(2027, 1, 15)


class TestSessionLog:
    def make_log(self):
        return SessionMileageLog(
            vehicle=VehicleCostParams(mpg=30, fuel_price_per_gallon=3.30,
                                      maintenance_per_mile=0.06,
                                      depreciation_per_mile=0.10),
            marginal_tax_rate=0.30)

    def test_entry_is_audit_defensible(self):
        log = self.make_log()
        e = log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                            (40.7, -74.4), (40.75, -74.42), 62.0, 148.0,
                            route_polyline="abc123")
        assert e.miles == 62.0
        assert e.business_purpose
        assert e.route_polyline == "abc123"
        assert e.start_loc and e.end_loc

    def test_validation(self):
        log = self.make_log()
        with pytest.raises(ValueError):
            log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 16, 0),
                            (0, 0), (0, 0), 10, 10)
        with pytest.raises(ValueError):
            log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 18, 0),
                            (0, 0), (0, 0), -1, 10)

    def test_deduction_uses_session_dates(self):
        log = self.make_log()
        log.log_session(datetime(2026, 6, 30, 17, 0), datetime(2026, 6, 30, 22, 0),
                        (0, 0), (0, 0), 100.0, 200.0)
        log.log_session(datetime(2026, 7, 1, 17, 0), datetime(2026, 7, 1, 22, 0),
                        (0, 0), (0, 0), 100.0, 200.0)
        assert log.deduction == pytest.approx(148.50)

    def test_shielded_income_counter(self):
        log = self.make_log()
        log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                        (0, 0), (0, 0), 62.0, 148.0)
        assert log.shielded_income == pytest.approx(62.0 * RATE_2026_H2, abs=0.01)

    def test_shielded_income_capped_at_gross(self):
        log = self.make_log()
        log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                        (0, 0), (0, 0), 300.0, 50.0)   # long deadhead day, low gross
        assert log.shielded_income == 50.0

    def test_true_net_composition(self):
        log = self.make_log()
        log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                        (0, 0), (0, 0), 62.0, 148.0)
        assert log.true_net == pytest.approx(
            148.0 - log.vehicle_cost - log.tax_reserve, abs=0.01)
        assert log.true_net < 148.0

    def test_true_net_hourly(self):
        log = self.make_log()
        log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                        (0, 0), (0, 0), 62.0, 148.0)
        assert log.true_net_hourly() == pytest.approx(log.true_net / 5.0, abs=0.01)

    def test_empty_log(self):
        log = self.make_log()
        assert log.total_miles == 0.0
        assert log.deduction == 0.0
        assert log.true_net == 0.0
        assert log.true_net_hourly() == 0.0

    def test_annual_estimate_wiring(self):
        log = self.make_log()
        log.log_session(datetime(2026, 8, 18, 17, 0), datetime(2026, 8, 18, 22, 0),
                        (0, 0), (0, 0), 62.0, 148.0)
        est = log.annual_estimate()
        assert est.net_profit == pytest.approx(148.0 - log.deduction, abs=0.01)
