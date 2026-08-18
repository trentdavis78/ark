"""Shift runner and the matched-baseline comparison.

Both policies replay the *same* generated offer stream, so the only thing that
differs is what they do with it. A busy driver does not see offers that arrive
mid-delivery — that opportunity cost is the whole reason the acceptance
threshold matters, and it is what a per-offer $/mi rule cannot reason about.

The reported figure is the PRD north star: net dollars per *online* hour,
after modeled true vehicle cost and after estimated tax reserve (PRD 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import TaxParams, VehicleCostParams
from ..mileage_tax import SessionMileageLog
from .policies import BlacktopPolicy, DollarPerMilePolicy, Policy
from .world import Market, SimOffer, ZoneProfile


@dataclass
class ShiftResult:
    policy_name: str
    online_minutes: float
    gross: float
    miles: float
    accepted: int
    declined: int
    missed_while_busy: int
    busy_minutes: float
    vehicle_cost: float
    tax_reserve: float
    shielded_income: float
    deduction: float

    @property
    def offers_seen(self) -> int:
        return self.accepted + self.declined

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.offers_seen if self.offers_seen else 0.0

    @property
    def utilization(self) -> float:
        return self.busy_minutes / self.online_minutes if self.online_minutes else 0.0

    @property
    def gross_hourly(self) -> float:
        return self.gross / self.online_minutes * 60.0 if self.online_minutes else 0.0

    @property
    def net(self) -> float:
        """Gross minus true vehicle cost minus tax reserve — the north star
        numerator. The mileage deduction shields part of the gross from tax,
        which is why net is not simply gross x (1 - rate)."""
        return self.gross - self.vehicle_cost - self.tax_reserve

    @property
    def net_hourly(self) -> float:
        return self.net / self.online_minutes * 60.0 if self.online_minutes else 0.0


# A 2019 Corolla-class gas vehicle at Aug-2026 NJ pump prices. cost_per_mile
# works out near $0.28, which at a ~20 mph blended average is ~$0.09/minute.
SIM_VEHICLE = VehicleCostParams(mpg=28.0, fuel_price_per_gallon=3.40)
SIM_BLENDED_MPH = 20.0
MARGINAL_COST_PER_MIN = SIM_VEHICLE.cost_per_mile * SIM_BLENDED_MPH / 60.0


def run_shift(offers: list[SimOffer], policy: Policy,
              start: datetime, end: datetime,
              vehicle: VehicleCostParams | None = None,
              tax: TaxParams | None = None) -> ShiftResult:
    """Replay one shift's offer stream under one policy."""
    vehicle = vehicle or SIM_VEHICLE
    log = SessionMileageLog(vehicle=vehicle, tax_params=tax or TaxParams())

    busy_until = start
    gross = miles = busy_minutes = 0.0
    accepted = declined = missed = 0

    for so in sorted(offers, key=lambda s: s.offer.seen_at):
        now = so.offer.seen_at
        if now < busy_until:
            missed += 1
            continue
        policy.observe_seen(so)
        remaining = (end - now).total_seconds() / 60.0
        if policy.decide(so, remaining):
            t = so.truth
            accepted += 1
            gross += t.true_total_payout
            miles += t.road_miles
            busy_minutes += t.total_minutes
            busy_until = now + timedelta(minutes=t.total_minutes)
            policy.observe_completed(so)
        else:
            declined += 1

    online_minutes = (end - start).total_seconds() / 60.0
    log.log_session(start, end, (40.85, -74.10), (40.85, -74.10), miles, gross)
    return ShiftResult(
        policy_name=policy.name,
        online_minutes=online_minutes,
        gross=gross,
        miles=miles,
        accepted=accepted,
        declined=declined,
        missed_while_busy=missed,
        busy_minutes=busy_minutes,
        vehicle_cost=log.vehicle_cost,
        tax_reserve=log.tax_reserve,
        shielded_income=log.shielded_income,
        deduction=log.deduction,
    )


@dataclass
class Comparison:
    baseline: ShiftResult
    blacktop: ShiftResult
    zone_label: str = "all zones"

    @property
    def net_hourly_lift(self) -> float:
        b = self.baseline.net_hourly
        return (self.blacktop.net_hourly - b) / b if b > 0 else 0.0

    @property
    def gross_hourly_lift(self) -> float:
        b = self.baseline.gross_hourly
        return (self.blacktop.gross_hourly - b) / b if b > 0 else 0.0


def _merge(results: list[ShiftResult], name: str) -> ShiftResult:
    """Sum shift results into a season total (online minutes add up: the
    driver works one zone at a time)."""
    return ShiftResult(
        policy_name=name,
        online_minutes=sum(r.online_minutes for r in results),
        gross=sum(r.gross for r in results),
        miles=sum(r.miles for r in results),
        accepted=sum(r.accepted for r in results),
        declined=sum(r.declined for r in results),
        missed_while_busy=sum(r.missed_while_busy for r in results),
        busy_minutes=sum(r.busy_minutes for r in results),
        vehicle_cost=sum(r.vehicle_cost for r in results),
        tax_reserve=sum(r.tax_reserve for r in results),
        shielded_income=sum(r.shielded_income for r in results),
        deduction=sum(r.deduction for r in results),
    )


def run_season(days: int = 30,
               hours_per_shift: float = 8.0,
               seed: int = 20260818,
               first_day: datetime | None = None,
               warmup_days: int = 5,
               zones: tuple[str, ...] = ("dense", "suburban", "sparse"),
               ) -> tuple[Comparison, dict[str, Comparison], BlacktopPolicy]:
    """Run a matched season: identical offer streams, two policies.

    ``warmup_days`` reproduces PRD Phase 0 — the driver runs their old
    heuristic while BLACKTOP logs and learns but issues no verdicts. Without
    it the comparison would unfairly charge the product for cold start it is
    explicitly designed to serve out of the logged baseline.
    """
    first_day = first_day or datetime(2026, 8, 3, 11, 0)   # a Monday
    market = Market(seed=seed)
    baseline = DollarPerMilePolicy()
    blacktop = BlacktopPolicy(marginal_cost_per_min=MARGINAL_COST_PER_MIN)

    per_zone_base: dict[str, list[ShiftResult]] = {z: [] for z in zones}
    per_zone_bt: dict[str, list[ShiftResult]] = {z: [] for z in zones}

    for day in range(warmup_days + days):
        for zkey in zones:
            zone = market.zone(zkey)
            start = first_day + timedelta(days=day)
            end = start + timedelta(hours=hours_per_shift)
            stream = market.stream(zone, start, end, prefix=f"d{day}")
            blacktop.record_presence(zone.hex_id, start, hours_per_shift)

            if day < warmup_days:
                # Phase 0: old heuristic drives; BLACKTOP observes only.
                _run_warmup(stream, baseline, blacktop, start, end)
                continue

            per_zone_base[zkey].append(run_shift(stream, baseline, start, end))
            per_zone_bt[zkey].append(run_shift(stream, blacktop, start, end))

    by_zone = {z: Comparison(_merge(per_zone_base[z], baseline.name),
                             _merge(per_zone_bt[z], blacktop.name),
                             zone_label=market.zone(z).label)
               for z in zones}
    overall = Comparison(
        _merge([r for z in zones for r in per_zone_base[z]], baseline.name),
        _merge([r for z in zones for r in per_zone_bt[z]], blacktop.name),
    )
    return overall, by_zone, blacktop


def _run_warmup(stream: list[SimOffer], baseline: DollarPerMilePolicy,
                blacktop: BlacktopPolicy, start: datetime, end: datetime) -> None:
    """Phase 0: decisions come from the incumbent heuristic; BLACKTOP only
    logs. Declines are logged too — they are observations, not absences."""
    busy_until = start
    for so in sorted(stream, key=lambda s: s.offer.seen_at):
        now = so.offer.seen_at
        if now < busy_until:
            continue
        blacktop.observe_seen(so)
        if baseline.decide(so, (end - now).total_seconds() / 60.0):
            blacktop.observe_completed(so)
            busy_until = now + timedelta(minutes=so.truth.total_minutes)
