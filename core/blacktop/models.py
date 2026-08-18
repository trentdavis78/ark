"""Shared data types for the BLACKTOP reference engine.

Every module in ``blacktop`` builds on these dataclasses so the whole engine
speaks one vocabulary. Mirrors the PRD 8 data model where a table exists.

Design notes
------------
* Pure stdlib. Frozen where the object is a value; mutable where it is a
  running accumulator (e.g. ``Session``).
* Money is ``float`` dollars, time is ``float`` minutes, distance is ``float``
  miles unless a field name says otherwise. Hours-of-week are ``int`` 0..167
  (0 = Monday 00:00, matching ``datetime.weekday()``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class Platform(enum.Enum):
    """Where the driver observed the offer. Labels on the driver's own
    observations only — never used for any network access (I2)."""
    DOORDASH = "doordash"
    UBER_EATS = "uber_eats"
    GRUBHUB = "grubhub"
    MANUAL = "manual"


class OfferType(enum.Enum):
    SINGLE = "single"
    STACKED = "stacked"
    SHOP_DELIVER = "shop_deliver"
    LARGE_ORDER = "large_order"


class VerdictColor(enum.Enum):
    """One glance, one color (PRD 9.1)."""
    GREEN = "green"    # take it
    AMBER = "amber"    # judgment call — within the indifference band
    RED = "red"        # skip it
    MANUAL_FALLBACK = "manual_fallback"  # parse confidence too low; no verdict emitted


class WeatherBucket(enum.Enum):
    CLEAR = "clear"
    RAIN = "rain"
    SNOW = "snow"
    SEVERE = "severe"


class DestinationClass(enum.Enum):
    """Dropoff class for friction modeling (F5) and tip features (F2)."""
    SINGLE_FAMILY = "single_family"
    MULTI_UNIT = "multi_unit"
    HIGH_RISE = "high_rise"
    COMMERCIAL = "commercial"
    CAMPUS = "campus"
    HOTEL = "hotel"
    UNKNOWN = "unknown"


def hour_of_week(dt: datetime) -> int:
    """0..167; 0 = Monday 00:00 local."""
    return dt.weekday() * 24 + dt.hour


# --------------------------------------------------------------------------- #
# Offers & verdicts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Offer:
    """A single offer card as observed on the driver's own screen (or entered
    manually). Mirrors PRD 8 ``offers``; declines are logged too."""
    offer_id: str
    platform: Platform
    seen_at: datetime
    displayed_payout: float
    merchant_name: str = ""
    merchant_address: str = ""
    merchant_id: str | None = None
    dropoff_address: str = ""
    dropoff_hex: str | None = None
    dropoff_class: DestinationClass = DestinationClass.UNKNOWN
    stated_distance_mi: float | None = None
    stated_minutes: float | None = None
    offer_type: OfferType = OfferType.SINGLE
    peak_pay: float = 0.0
    hit_display_cap: bool = False
    item_count: int | None = None
    subtotal: float | None = None


@dataclass(frozen=True)
class TimeBreakdown:
    """The five-term time model of F1, minutes each. ``total_minutes`` is the
    denominator of the projected hourly value."""
    drive_to_merchant_min: float
    merchant_wait_min: float
    drive_to_customer_min: float
    dropoff_friction_min: float
    return_to_density_min: float

    @property
    def total_minutes(self) -> float:
        return (self.drive_to_merchant_min + self.merchant_wait_min
                + self.drive_to_customer_min + self.dropoff_friction_min
                + self.return_to_density_min)


@dataclass(frozen=True)
class Verdict:
    """Advisory output of F1. This object is rendered and spoken — it is never
    consumed by anything that acts (I3/I4)."""
    color: VerdictColor
    projected_net_hourly: float
    reservation_rate_hourly: float
    expected_payout: float
    expected_hidden_tip: float
    time_breakdown: TimeBreakdown | None
    parse_confidence: float
    tts_text: str

    @property
    def is_actionable(self) -> bool:
        return self.color is not VerdictColor.MANUAL_FALLBACK


@dataclass(frozen=True)
class Delivery:
    """Completed delivery — the free training label for F2 (actual payout vs.
    displayed) and the dwell observation for F4. Mirrors PRD 8 ``deliveries``."""
    delivery_id: str
    offer: Offer
    accepted_at: datetime
    merchant_arrive_at: datetime | None
    merchant_depart_at: datetime | None
    dropoff_at: datetime
    actual_payout: float
    actual_miles: float | None = None

    @property
    def actual_tip_delta(self) -> float:
        """Concealed portion realized: actual − displayed (>= 0 in practice)."""
        return self.actual_payout - self.offer.displayed_payout

    @property
    def merchant_wait_minutes(self) -> float | None:
        if self.merchant_arrive_at is None or self.merchant_depart_at is None:
            return None
        return (self.merchant_depart_at - self.merchant_arrive_at).total_seconds() / 60.0


# --------------------------------------------------------------------------- #
# Session & mileage
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    """Online-to-offline work session. Mirrors PRD 8 ``sessions``."""
    session_id: str
    driver_id: str
    started_at: datetime
    ended_at: datetime | None = None
    start_loc: tuple[float, float] | None = None   # (lat, lng)
    end_loc: tuple[float, float] | None = None
    total_miles: float = 0.0
    gross_earnings: float = 0.0
    platform_mix: dict[str, float] = field(default_factory=dict)
    planned_end: datetime | None = None

    def remaining_minutes(self, now: datetime) -> float | None:
        if self.planned_end is None:
            return None
        return max(0.0, (self.planned_end - now).total_seconds() / 60.0)


@dataclass(frozen=True)
class MileageLogEntry:
    """One audit-defensible mileage log row (F7): date, start/end time and
    location, miles, business purpose, route polyline."""
    date: datetime
    start_time: datetime
    end_time: datetime
    start_loc: tuple[float, float]
    end_loc: tuple[float, float]
    miles: float
    business_purpose: str
    route_polyline: str = ""


# --------------------------------------------------------------------------- #
# Learned stores
# --------------------------------------------------------------------------- #

@dataclass
class Merchant:
    """Per-location (not per-brand) merchant record. Mirrors PRD 8 ``merchants``."""
    merchant_id: str
    platform_name: str
    address: str
    hex_id: str
    lat: float | None = None
    lng: float | None = None
    sample_n: int = 0


@dataclass
class Building:
    """Last-100-feet knowledge node (F5). Mirrors PRD 8 ``buildings``."""
    building_id: str
    hex_res9: str
    label: str
    destination_class: DestinationClass = DestinationClass.UNKNOWN
    access_notes: str = ""
    gate_code_encrypted: bytes | None = None
    entry_door: str = ""
    elevator_bank: str = ""
    lobby_handoff_ok: bool | None = None
    dock_access: bool | None = None
    safe_park_note: str = ""
    friction_minutes_p50: float | None = None
    shareable: bool = False          # defaults False; see building_intel.set_shareable
    sample_n: int = 0


@dataclass
class ZoneStats:
    """Per hex x hour-of-week x weather bucket stats. Mirrors PRD 8 ``zone_stats``."""
    hex_id: str
    hour_of_week: int
    weather_bucket: WeatherBucket
    offer_arrival_rate_per_hr: float = 0.0
    value_mean_per_min: float = 0.0     # mean offer $/projected-minute
    value_var_per_min: float = 0.0
    expected_net_hourly: float = 0.0
    sample_n: int = 0


# --------------------------------------------------------------------------- #
# Cost & tax parameters (F7)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VehicleCostParams:
    """User-supplied vehicle economics -> true cost per mile."""
    mpg: float | None = None                 # gas vehicle
    fuel_price_per_gallon: float | None = None
    kwh_per_mile: float | None = None        # EV
    electricity_price_per_kwh: float | None = None
    maintenance_per_mile: float = 0.06
    depreciation_per_mile: float = 0.10

    @property
    def energy_cost_per_mile(self) -> float:
        if self.mpg and self.fuel_price_per_gallon:
            return self.fuel_price_per_gallon / self.mpg
        if self.kwh_per_mile and self.electricity_price_per_kwh:
            return self.kwh_per_mile * self.electricity_price_per_kwh
        return 0.0

    @property
    def cost_per_mile(self) -> float:
        return self.energy_cost_per_mile + self.maintenance_per_mile + self.depreciation_per_mile


@dataclass(frozen=True)
class TaxParams:
    """2026 defaults, single filer, New Jersey. Every number is overridable —
    these are estimates for planning, not tax advice."""
    se_tax_rate: float = 0.153               # 12.4% SS + 2.9% Medicare
    se_net_earnings_factor: float = 0.9235
    ss_wage_base: float = 184_500.0          # 2026 Social Security wage base (est.)
    ss_rate: float = 0.124
    medicare_rate: float = 0.029
    federal_standard_deduction: float = 16_100.0   # 2026 single (est.)
    # (upper bound, rate) pairs, 2026 single brackets (est.), applied to taxable income
    federal_brackets: tuple[tuple[float, float], ...] = (
        (12_400.0, 0.10), (50_400.0, 0.12), (105_700.0, 0.22),
        (201_775.0, 0.24), (256_225.0, 0.32), (640_600.0, 0.35),
        (float("inf"), 0.37),
    )
    # NJ single-filer brackets (statutory, unchanged for years)
    nj_brackets: tuple[tuple[float, float], ...] = (
        (20_000.0, 0.014), (35_000.0, 0.0175), (40_000.0, 0.035),
        (75_000.0, 0.05525), (500_000.0, 0.0637), (1_000_000.0, 0.0897),
        (float("inf"), 0.1075),
    )
    nj_filing_threshold: float = 10_000.0    # no NJ tax if gross income below this
