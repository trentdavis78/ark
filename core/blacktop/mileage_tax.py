"""F7 — Mileage & Tax Engine.

Automatic session-boundary mileage logging (all miles: delivery legs,
deadhead, repositioning), the 2026 split IRS rate, an audit-defensible log,
live "true net", the shielded-income counter, SE tax, and federal + NJ
quarterly estimate calculators, plus the vehicle cost model.

Everything here is deterministic arithmetic on stated parameters; defaults
live in ``models.TaxParams`` / ``models.VehicleCostParams`` and every number
is overridable. Estimates for planning — not tax advice.

2026 IRS standard mileage rate (PRD F7 hard requirement):
    72.5 cents/mile for miles driven on or before 2026-06-30
    76.0 cents/mile for miles driven on or after  2026-07-01
A tracker applying a single annual rate to 2026 is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import MileageLogEntry, TaxParams, VehicleCostParams

RATE_2026_H1 = 0.725      # through June 30, 2026
RATE_2026_H2 = 0.76       # from July 1, 2026
_2026_SPLIT = date(2026, 7, 1)

# Estimated-payment due dates for tax year 2026 (federal & NJ share them).
QUARTERLY_DUE_DATES_2026 = (
    date(2026, 4, 15), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15),
)


def mileage_rate(d: date) -> float:
    """IRS standard mileage rate applicable to miles driven on date ``d``.
    Only 2026's split is modeled; other years raise so a wrong rate can never
    be silently applied."""
    if d.year != 2026:
        raise ValueError(f"rate table only covers 2026, got {d.year}")
    return RATE_2026_H1 if d < _2026_SPLIT else RATE_2026_H2


def mileage_deduction(miles_by_date: dict[date, float]) -> float:
    """Deduction for a {date: miles} map, applying the correct per-date rate."""
    total = 0.0
    for d, miles in miles_by_date.items():
        if miles < 0:
            raise ValueError("negative miles")
        total += miles * mileage_rate(d)
    return round(total, 2)


def _bracket_tax(taxable: float, brackets: tuple[tuple[float, float], ...]) -> float:
    """Progressive tax over (upper_bound, rate) brackets."""
    if taxable <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        span = min(taxable, upper) - lower
        if span <= 0:
            break
        tax += span * rate
        lower = upper
    return tax


# --------------------------------------------------------------------- taxes

@dataclass(frozen=True)
class TaxEstimate:
    net_profit: float                 # schedule C: gross - mileage - other expenses
    se_taxable_earnings: float        # net_profit * 0.9235
    se_tax: float
    federal_taxable_income: float
    federal_income_tax: float
    nj_taxable_income: float
    nj_income_tax: float

    @property
    def total_tax(self) -> float:
        return self.se_tax + self.federal_income_tax + self.nj_income_tax

    @property
    def quarterly_payment(self) -> float:
        return round(self.total_tax / 4.0, 2)


def self_employment_tax(net_profit: float, params: TaxParams = TaxParams()) -> float:
    """SE tax: 15.3% on 92.35% of net profit — 12.4% Social Security capped
    at the wage base, 2.9% Medicare uncapped."""
    earnings = max(0.0, net_profit) * params.se_net_earnings_factor
    ss = min(earnings, params.ss_wage_base) * params.ss_rate
    medicare = earnings * params.medicare_rate
    return ss + medicare


def estimate_taxes(gross_income: float,
                   mileage_deduction_amount: float,
                   other_expenses: float = 0.0,
                   params: TaxParams = TaxParams()) -> TaxEstimate:
    """Full-year estimate: Schedule C net -> SE tax -> federal (standard
    deduction + half-SE deduction) -> NJ (no federal-style standard deduction;
    filing threshold instead)."""
    net_profit = max(0.0, gross_income - mileage_deduction_amount - other_expenses)
    se = self_employment_tax(net_profit, params)
    half_se = se / 2.0

    fed_taxable = max(0.0, net_profit - half_se - params.federal_standard_deduction)
    fed_tax = _bracket_tax(fed_taxable, params.federal_brackets)

    # NJ: mileage/business expenses reduce NJ net profits from business; the
    # federal standard deduction and half-SE deduction do NOT apply.
    nj_taxable = net_profit
    nj_tax = 0.0 if nj_taxable < params.nj_filing_threshold \
        else _bracket_tax(nj_taxable, params.nj_brackets)

    return TaxEstimate(
        net_profit=round(net_profit, 2),
        se_taxable_earnings=round(net_profit * params.se_net_earnings_factor, 2),
        se_tax=round(se, 2),
        federal_taxable_income=round(fed_taxable, 2),
        federal_income_tax=round(fed_tax, 2),
        nj_taxable_income=round(nj_taxable, 2),
        nj_income_tax=round(nj_tax, 2),
    )


def quarterly_schedule(est: TaxEstimate) -> list[tuple[date, float]]:
    """1040-ES / NJ-1040-ES: four equal installments on the statutory dates."""
    return [(d, est.quarterly_payment) for d in QUARTERLY_DUE_DATES_2026]


# ------------------------------------------------------------------ sessions

@dataclass
class SessionMileageLog:
    """Session-boundary mileage capture (online -> offline). Accumulates an
    audit-defensible entry per session and answers the live-display questions:
    true net, shielded income, tax reserve."""
    vehicle: VehicleCostParams = field(default_factory=VehicleCostParams)
    tax_params: TaxParams = field(default_factory=TaxParams)
    marginal_tax_rate: float = 0.30    # SE + marginal fed + NJ, for live reserve
    entries: list[MileageLogEntry] = field(default_factory=list)
    gross_earnings: float = 0.0

    def log_session(self,
                    start_time: datetime, end_time: datetime,
                    start_loc: tuple[float, float], end_loc: tuple[float, float],
                    miles: float, gross: float,
                    route_polyline: str = "",
                    business_purpose: str = "delivery driving — all online miles") -> MileageLogEntry:
        if end_time <= start_time:
            raise ValueError("session end must be after start")
        if miles < 0 or gross < 0:
            raise ValueError("negative miles or gross")
        entry = MileageLogEntry(
            date=start_time, start_time=start_time, end_time=end_time,
            start_loc=start_loc, end_loc=end_loc, miles=miles,
            business_purpose=business_purpose, route_polyline=route_polyline)
        self.entries.append(entry)
        self.gross_earnings += gross
        return entry

    # ------------------------------------------------------------- rollups
    @property
    def total_miles(self) -> float:
        return sum(e.miles for e in self.entries)

    def miles_by_date(self) -> dict[date, float]:
        out: dict[date, float] = {}
        for e in self.entries:
            d = e.date.date()
            out[d] = out.get(d, 0.0) + e.miles
        return out

    @property
    def deduction(self) -> float:
        return mileage_deduction(self.miles_by_date())

    @property
    def shielded_income(self) -> float:
        """The shielded-income counter (PRD F7): how much of gross is tax-free
        because the mileage deduction offsets it. Capped at gross — you can't
        shield income you didn't earn."""
        return round(min(self.gross_earnings, self.deduction), 2)

    @property
    def vehicle_cost(self) -> float:
        return round(self.total_miles * self.vehicle.cost_per_mile, 2)

    @property
    def tax_reserve(self) -> float:
        """Live reserve: marginal rate applied to the unshielded portion."""
        unshielded = max(0.0, self.gross_earnings - self.deduction)
        return round(unshielded * self.marginal_tax_rate, 2)

    @property
    def true_net(self) -> float:
        """Gross minus modeled true vehicle cost minus estimated tax reserve —
        the number the whole product optimizes (PRD 4 north star)."""
        return round(self.gross_earnings - self.vehicle_cost - self.tax_reserve, 2)

    def true_net_hourly(self) -> float:
        minutes = sum((e.end_time - e.start_time).total_seconds() / 60.0
                      for e in self.entries)
        return self.true_net / minutes * 60.0 if minutes > 0 else 0.0

    def annual_estimate(self, other_expenses: float = 0.0) -> TaxEstimate:
        return estimate_taxes(self.gross_earnings, self.deduction,
                              other_expenses, self.tax_params)
