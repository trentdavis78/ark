from datetime import datetime

from blacktop.models import (
    DestinationClass, Offer, Platform, TaxParams, TimeBreakdown,
    VehicleCostParams, Session, hour_of_week,
)


def test_hour_of_week_monday_midnight_is_zero():
    assert hour_of_week(datetime(2026, 8, 17, 0, 0)) == 0   # a Monday


def test_hour_of_week_range_and_tuesday_evening():
    dt = datetime(2026, 8, 18, 18, 30)   # Tuesday 18:00 -> 24+18
    assert hour_of_week(dt) == 42
    assert 0 <= hour_of_week(dt) <= 167


def test_time_breakdown_total():
    tb = TimeBreakdown(5, 4, 10, 2, 6)
    assert tb.total_minutes == 27


def test_vehicle_cost_gas():
    v = VehicleCostParams(mpg=30, fuel_price_per_gallon=3.0,
                          maintenance_per_mile=0.06, depreciation_per_mile=0.10)
    assert abs(v.energy_cost_per_mile - 0.10) < 1e-9
    assert abs(v.cost_per_mile - 0.26) < 1e-9


def test_vehicle_cost_ev():
    v = VehicleCostParams(kwh_per_mile=0.28, electricity_price_per_kwh=0.17)
    assert abs(v.energy_cost_per_mile - 0.0476) < 1e-6


def test_vehicle_cost_no_energy_params():
    v = VehicleCostParams()
    assert v.energy_cost_per_mile == 0.0
    assert v.cost_per_mile == v.maintenance_per_mile + v.depreciation_per_mile


def test_offer_defaults():
    o = Offer("id", Platform.DOORDASH, datetime(2026, 8, 18, 18, 0), 7.5)
    assert o.peak_pay == 0.0
    assert not o.hit_display_cap
    assert o.dropoff_class is DestinationClass.UNKNOWN


def test_session_remaining_minutes():
    s = Session("s", "d", datetime(2026, 8, 18, 17, 0),
                planned_end=datetime(2026, 8, 18, 22, 0))
    assert s.remaining_minutes(datetime(2026, 8, 18, 21, 0)) == 60.0
    assert s.remaining_minutes(datetime(2026, 8, 18, 23, 0)) == 0.0
    assert Session("s", "d", datetime(2026, 8, 18, 17, 0)).remaining_minutes(
        datetime(2026, 8, 18, 18, 0)) is None


def test_tax_params_brackets_monotone():
    p = TaxParams()
    uppers = [u for u, _ in p.federal_brackets]
    assert uppers == sorted(uppers)
    nj_uppers = [u for u, _ in p.nj_brackets]
    assert nj_uppers == sorted(nj_uppers)
