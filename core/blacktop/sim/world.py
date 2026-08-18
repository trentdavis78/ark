"""Generative model of a North/Central NJ delivery market.

The simulator exists to answer the one question the PRD makes falsifiable:
does the decision layer actually lift net $/hr, and *through which mechanism*?
(PRD 10, Phase 3 exit criterion; PRD 11, "the lift doesn't materialize".)

The world holds ground truth. The engine never sees it. The engine sees only
an ``Offer`` — the card — which deliberately reproduces the three distortions
named in PRD 1:

* the tip is withheld above a market display cap,
* travel is stated as straight-line distance, not road distance,
* pickup wait is stated as zero.

Everything else the engine must infer from its own logged observations, which
is the entire point of the product.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import DestinationClass, Offer, OfferType, Platform

# Market-wide display cap. PRD F2/12.1: DoorDash appears to cap the displayed
# amount per market and appends "total may be higher". The cap value is the
# highest-signal feature in the tip model and is detectable by counting alone.
DEFAULT_DISPLAY_CAP = 12.00

STRAIGHT_LINE_FACTOR = 0.78   # card distance / true road distance
STATED_TIME_OPTIMISM = 0.90   # card minutes / true driving minutes (no wait term)


@dataclass(frozen=True)
class ZoneProfile:
    """One operating zone. The three archetypes in the NJ playbook: a dense
    low-tip corridor, an affluent suburban pocket, and a sparse long-haul
    fringe. The correct acceptance threshold differs by ~3x across them, which
    is exactly what a fixed $/mi rule cannot express."""
    key: str
    hex_id: str
    label: str
    arrival_rate_per_hr: float
    base_pay_range: tuple[float, float]
    tip_mu: float                  # lognormal mu of the tip distribution
    tip_sigma: float
    delivery_miles_mean: float
    speed_mph: float
    deadhead_minutes: float        # T_return_to_density from this zone
    dest_mix: tuple[tuple[DestinationClass, float], ...]


NORTH_JERSEY: tuple[ZoneProfile, ...] = (
    ZoneProfile(
        key="dense", hex_id="g8:5926:-10725", label="Paramus / Rt 17 corridor",
        arrival_rate_per_hr=24.0, base_pay_range=(2.00, 3.25),
        tip_mu=1.38, tip_sigma=0.50,          # ~$3.97 median tip
        delivery_miles_mean=2.4, speed_mph=20.0, deadhead_minutes=3.0,
        dest_mix=((DestinationClass.MULTI_UNIT, 0.40),
                  (DestinationClass.SINGLE_FAMILY, 0.30),
                  (DestinationClass.COMMERCIAL, 0.20),
                  (DestinationClass.HIGH_RISE, 0.10)),
    ),
    ZoneProfile(
        key="suburban", hex_id="g8:5915:-10740", label="Morristown / Chatham",
        arrival_rate_per_hr=12.0, base_pay_range=(2.25, 3.75),
        tip_mu=2.02, tip_sigma=0.55,          # ~$7.54 median tip
        delivery_miles_mean=3.6, speed_mph=26.0, deadhead_minutes=7.0,
        dest_mix=((DestinationClass.SINGLE_FAMILY, 0.65),
                  (DestinationClass.MULTI_UNIT, 0.20),
                  (DestinationClass.COMMERCIAL, 0.15)),
    ),
    ZoneProfile(
        key="sparse", hex_id="g8:5890:-10770", label="Hunterdon fringe",
        arrival_rate_per_hr=5.0, base_pay_range=(3.00, 5.50),
        tip_mu=1.88, tip_sigma=0.60,
        delivery_miles_mean=7.8, speed_mph=38.0, deadhead_minutes=19.0,
        dest_mix=((DestinationClass.SINGLE_FAMILY, 0.85),
                  (DestinationClass.COMMERCIAL, 0.15)),
    ),
)

# Dropoff friction by destination class (ground truth minutes, mean).
FRICTION_MEAN = {
    DestinationClass.SINGLE_FAMILY: 1.6,
    DestinationClass.MULTI_UNIT: 4.2,
    DestinationClass.HIGH_RISE: 7.5,
    DestinationClass.COMMERCIAL: 3.0,
    DestinationClass.CAMPUS: 6.5,
    DestinationClass.HOTEL: 5.0,
    DestinationClass.UNKNOWN: 3.0,
}


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    name: str
    category: str
    zone_key: str
    wait_mean_min: float     # ground-truth pickup wait, per *location*
    wait_sigma: float

    def draw_wait(self, rng: random.Random, dinner_rush: bool) -> float:
        mult = 1.55 if dinner_rush else 1.0
        w = rng.lognormvariate(math.log(max(0.5, self.wait_mean_min * mult)), self.wait_sigma)
        return min(w, 45.0)


# Per-location, not per-brand: the same chain four miles apart behaves
# completely differently (PRD F4).
MERCHANTS: tuple[Merchant, ...] = (
    Merchant("m_wendys_17",   "Wendy's — Rt 17",        "fast_food", "dense",    4.0, 0.35),
    Merchant("m_chipotle_gwc","Chipotle — Garden State","fast_casual","dense",   5.5, 0.40),
    Merchant("m_cheesecake",  "Cheesecake Factory",     "casual_dining","dense",13.5, 0.45),
    Merchant("m_popeyes_4",   "Popeyes — Rt 4",         "fast_food", "dense",   11.0, 0.50),
    Merchant("m_starbucks_p", "Starbucks — Paramus",    "cafe",      "dense",    3.0, 0.30),
    Merchant("m_shoprite_pa", "ShopRite — Paramus",     "grocery",   "dense",   14.0, 0.35),
    Merchant("m_taco_17",     "Taco Bell — Rt 17",       "fast_food", "dense",    3.4, 0.30),
    Merchant("m_mcd_4",       "McDonald's — Rt 4",       "fast_food", "dense",    2.8, 0.28),
    Merchant("m_dunkin_pa",   "Dunkin — Paramus",        "cafe",      "dense",    2.5, 0.28),
    Merchant("m_five_guys",   "Five Guys — Garden State","fast_casual","dense",   6.0, 0.35),
    Merchant("m_mtown_tavern","Morristown Tavern",      "casual_dining","suburban",9.0,0.40),
    Merchant("m_panera_mt",   "Panera — Morristown",    "fast_casual","suburban",4.5, 0.35),
    Merchant("m_sushi_chat",  "Chatham Sushi",          "casual_dining","suburban",8.0,0.45),
    Merchant("m_wendys_mt",   "Wendy's — Morristown",   "fast_food", "suburban", 3.2, 0.30),
    Merchant("m_chipotle_mt", "Chipotle — Morristown",   "fast_casual","suburban",4.0, 0.32),
    Merchant("m_dunkin_chat", "Dunkin — Chatham",        "cafe",      "suburban", 2.6, 0.28),
    Merchant("m_diner_flem",  "Flemington Diner",       "casual_dining","sparse", 7.0, 0.40),
    Merchant("m_pizza_hunt",  "Hunterdon Pizza",        "casual_dining","sparse", 6.0, 0.40),
)


@dataclass(frozen=True)
class GroundTruth:
    """What actually happens if the driver takes the offer. Never visible to
    the engine — only realized after acceptance, exactly as in the field."""
    true_total_payout: float
    hidden_tip: float
    drive_to_merchant_min: float
    merchant_wait_min: float
    drive_to_customer_min: float
    dropoff_friction_min: float
    deadhead_min: float
    road_miles: float

    @property
    def total_minutes(self) -> float:
        return (self.drive_to_merchant_min + self.merchant_wait_min
                + self.drive_to_customer_min + self.dropoff_friction_min
                + self.deadhead_min)

    @property
    def realized_rate_per_min(self) -> float:
        return self.true_total_payout / max(self.total_minutes, 1e-9)


@dataclass(frozen=True)
class SimOffer:
    """An offer card plus the ground truth behind it."""
    offer: Offer
    truth: GroundTruth
    zone: ZoneProfile
    merchant: Merchant


@dataclass
class Market:
    """Poisson offer generator over the zone/merchant mix."""
    zones: tuple[ZoneProfile, ...] = NORTH_JERSEY
    merchants: tuple[Merchant, ...] = MERCHANTS
    display_cap: float = DEFAULT_DISPLAY_CAP
    peak_pay_dinner: float = 1.50
    seed: int = 20260818
    rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._by_zone: dict[str, list[Merchant]] = {}
        for m in self.merchants:
            self._by_zone.setdefault(m.zone_key, []).append(m)

    # ------------------------------------------------------------- helpers
    def zone(self, key: str) -> ZoneProfile:
        for z in self.zones:
            if z.key == key:
                return z
        raise KeyError(key)

    @staticmethod
    def is_dinner_rush(when: datetime) -> bool:
        return 17 <= when.hour < 21

    def demand_multiplier(self, when: datetime) -> float:
        """Offer flow is strongly bimodal: lunch and dinner."""
        h = when.hour + when.minute / 60.0
        lunch = math.exp(-((h - 12.3) ** 2) / 1.6)
        dinner = math.exp(-((h - 18.6) ** 2) / 3.2)
        return 0.28 + 1.55 * dinner + 0.80 * lunch

    def next_arrival_minutes(self, zone: ZoneProfile, when: datetime) -> float:
        """Exponential interarrival at the zone's time-varying rate."""
        rate_hr = max(0.15, zone.arrival_rate_per_hr * self.demand_multiplier(when))
        return self.rng.expovariate(rate_hr / 60.0)

    # ------------------------------------------------------------ creation
    def draw_offer(self, zone: ZoneProfile, when: datetime, offer_id: str) -> SimOffer:
        rng = self.rng
        merchant = rng.choice(self._by_zone[zone.key])
        rush = self.is_dinner_rush(when)

        base = rng.uniform(*zone.base_pay_range)
        tip = rng.lognormvariate(zone.tip_mu, zone.tip_sigma)
        # Long hauls out of the sparse fringe attract a modest distance premium
        # from the platform, but never enough to cover the deadhead home.
        road_miles = max(0.6, rng.gauss(zone.delivery_miles_mean, zone.delivery_miles_mean * 0.35))
        base += 0.18 * max(0.0, road_miles - 4.0)
        peak = self.peak_pay_dinner if rush and rng.random() < 0.45 else 0.0

        # Money is whole cents everywhere, so the displayed figure and the
        # concealed remainder reconcile exactly against the true payout.
        true_total = round(base + peak + tip, 2)
        displayed = round(min(true_total, self.display_cap), 2)
        at_cap = true_total > self.display_cap
        hidden = round(true_total - displayed, 2)

        dest_class = self._draw_dest(zone)
        to_merchant = max(1.0, rng.gauss(3.5, 1.3)) if zone.key != "sparse" else max(2.0, rng.gauss(8.5, 3.0))
        wait = merchant.draw_wait(rng, rush)
        to_customer = road_miles / zone.speed_mph * 60.0
        friction = max(0.3, rng.gauss(FRICTION_MEAN[dest_class], FRICTION_MEAN[dest_class] * 0.35))
        deadhead = max(0.5, rng.gauss(zone.deadhead_minutes, zone.deadhead_minutes * 0.30))

        truth = GroundTruth(
            true_total_payout=true_total, hidden_tip=hidden,
            drive_to_merchant_min=to_merchant, merchant_wait_min=wait,
            drive_to_customer_min=to_customer, dropoff_friction_min=friction,
            deadhead_min=deadhead,
            # Business miles are all online miles: to the merchant, to the
            # customer, and back toward density (PRD F7).
            road_miles=road_miles + to_merchant / 60.0 * zone.speed_mph
                       + deadhead / 60.0 * zone.speed_mph,
        )

        offer = Offer(
            offer_id=offer_id,
            platform=Platform.DOORDASH,
            seen_at=when,
            displayed_payout=displayed,
            merchant_name=merchant.name,
            merchant_id=merchant.merchant_id,
            dropoff_address=f"{rng.randint(1, 900)} {zone.label} #{rng.randint(1, 40)}",
            dropoff_hex=zone.hex_id,
            dropoff_class=dest_class,
            # The card's two lies: straight-line distance, and a time estimate
            # that silently assumes the food is already on the counter.
            stated_distance_mi=round(road_miles * STRAIGHT_LINE_FACTOR, 1),
            stated_minutes=round((to_merchant + to_customer) * STATED_TIME_OPTIMISM),
            offer_type=OfferType.SINGLE if merchant.category != "grocery" else OfferType.SHOP_DELIVER,
            peak_pay=peak,
            hit_display_cap=at_cap,
        )
        return SimOffer(offer=offer, truth=truth, zone=zone, merchant=merchant)

    def _draw_dest(self, zone: ZoneProfile) -> DestinationClass:
        r = self.rng.random()
        acc = 0.0
        for cls, p in zone.dest_mix:
            acc += p
            if r <= acc:
                return cls
        return zone.dest_mix[-1][0]

    # ------------------------------------------------------------- streams
    def stream(self, zone: ZoneProfile, start: datetime,
               end: datetime, prefix: str = "o") -> list[SimOffer]:
        """All offers the zone emits in the window, in time order. Generated
        once and replayed against every policy, so the comparison is matched:
        the policies differ only in what they *do*, never in what they see."""
        out: list[SimOffer] = []
        t = start
        i = 0
        while True:
            t = t + timedelta(minutes=self.next_arrival_minutes(zone, t))
            if t >= end:
                return out
            i += 1
            out.append(self.draw_offer(zone, t, f"{prefix}-{zone.key}-{i}"))
