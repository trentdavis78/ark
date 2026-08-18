"""F3 — Zone Alpha Map: expected net $/hr by hex x hour-of-week x weather.

Hotspot maps show offer *density*; this shows offer *quality* density,
learned from the driver's own offer stream — declines included, because a
declined offer is still an observation about that hex.

Hex abstraction
---------------
The PRD specifies H3 (res 8 primary, res 9 buildings), but the H3 C library
is not a dependency of the reference engine. ``HexIndex`` is a small protocol;
``GridHexIndex`` is a pure-python lat/lng grid with the same interface
(cell id, neighbors, distance). Production swaps in real H3 without touching
zone logic.

Outputs (PRD F3):
* per-cell expected net hourly + arrival rate (``ZoneStats``)
* positioning recommendation with expected gain, drive cost, break-even
* seam detection between high-volume and high-value clusters
* the deadhead surface T_return_to_density that F1 consumes
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol

from .models import WeatherBucket, ZoneStats, hour_of_week

EARTH_MI_PER_DEG_LAT = 69.0


class HexIndex(Protocol):
    """Minimal hex-grid interface (H3-compatible shape)."""
    def cell(self, lat: float, lng: float) -> str: ...
    def neighbors(self, cell_id: str) -> list[str]: ...
    def distance_miles(self, cell_a: str, cell_b: str) -> float: ...
    def center(self, cell_id: str) -> tuple[float, float]: ...


class GridHexIndex:
    """Pure-python square-grid stand-in for H3 res 8 (~0.46 mi across).
    Cell ids look like ``g8:40712:-7404``."""

    def __init__(self, cell_miles: float = 0.46, tag: str = "g8") -> None:
        self.cell_miles = cell_miles
        self.tag = tag
        self._deg_lat = cell_miles / EARTH_MI_PER_DEG_LAT

    def _deg_lng(self, lat: float) -> float:
        return self.cell_miles / (EARTH_MI_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat))))

    def cell(self, lat: float, lng: float) -> str:
        row = math.floor(lat / self._deg_lat)
        col = math.floor(lng / self._deg_lng(lat))
        return f"{self.tag}:{row}:{col}"

    def _parse(self, cell_id: str) -> tuple[int, int]:
        _, row, col = cell_id.split(":")
        return int(row), int(col)

    def center(self, cell_id: str) -> tuple[float, float]:
        row, col = self._parse(cell_id)
        lat = (row + 0.5) * self._deg_lat
        lng = (col + 0.5) * self._deg_lng(lat)
        return lat, lng

    def neighbors(self, cell_id: str) -> list[str]:
        row, col = self._parse(cell_id)
        return [f"{self.tag}:{row + dr}:{col + dc}"
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if not (dr == 0 and dc == 0)]

    def distance_miles(self, cell_a: str, cell_b: str) -> float:
        ra, ca = self._parse(cell_a)
        rb, cb = self._parse(cell_b)
        return math.hypot(ra - rb, ca - cb) * self.cell_miles


@dataclass(frozen=True)
class PositioningAdvice:
    """PRD F3: 'Move 2.4mi to hex X. Expected +$8.40/hr. Drive cost 7 min.
    Break-even at 21 min remaining in session.'"""
    target_hex: str
    distance_miles: float
    drive_cost_minutes: float
    expected_gain_per_hr: float
    break_even_minutes: float

    def render(self) -> str:
        return (f"Move {self.distance_miles:.1f}mi to hex {self.target_hex}. "
                f"Expected +${self.expected_gain_per_hr:.2f}/hr. "
                f"Drive cost {self.drive_cost_minutes:.0f} min. "
                f"Break-even at {self.break_even_minutes:.0f} min remaining in session.")


@dataclass(frozen=True)
class Seam:
    """Boundary position between a high-volume cluster and a high-value
    cluster — the highest-value positioning play in this region."""
    volume_hex: str
    value_hex: str
    volume_rate_per_hr: float
    value_hourly: float


class ZoneMap:
    def __init__(self, hex_index: HexIndex | None = None,
                 avg_speed_mph: float = 25.0,
                 min_arrival_rate_per_hr: float = 4.0) -> None:
        self.hex = hex_index or GridHexIndex()
        self.avg_speed_mph = avg_speed_mph
        self.min_arrival_rate_per_hr = min_arrival_rate_per_hr
        self._stats: dict[tuple[str, int, WeatherBucket], ZoneStats] = {}
        # observation counts to derive arrival rates: hours observed per key
        self._hours_observed: dict[tuple[str, int, WeatherBucket], float] = defaultdict(float)

    # ------------------------------------------------------------ learning
    def _key(self, hex_id: str, when: datetime,
             weather: WeatherBucket) -> tuple[str, int, WeatherBucket]:
        return (hex_id, hour_of_week(when), weather)

    def record_presence(self, hex_id: str, when: datetime,
                        weather: WeatherBucket, hours: float) -> None:
        """Time spent online in a hex — the denominator for arrival rate."""
        self._hours_observed[self._key(hex_id, when, weather)] += hours

    def record_offer(self, hex_id: str, when: datetime, weather: WeatherBucket,
                     value_per_min: float) -> None:
        """Every offer seen in the hex — accepted or declined."""
        key = self._key(hex_id, when, weather)
        st = self._stats.get(key)
        if st is None:
            st = ZoneStats(hex_id=hex_id, hour_of_week=key[1], weather_bucket=weather)
            self._stats[key] = st
        # Welford-style running mean/variance on value_per_min.
        st.sample_n += 1
        delta = value_per_min - st.value_mean_per_min
        st.value_mean_per_min += delta / st.sample_n
        st.value_var_per_min += delta * (value_per_min - st.value_mean_per_min)
        hours = self._hours_observed.get(key, 0.0)
        st.offer_arrival_rate_per_hr = st.sample_n / hours if hours > 0 else 0.0
        # Quality density: value rate ($/min -> $/hr) discounted by how much
        # of an hour is actually covered by offers at this arrival rate
        # (utilization ~= min(1, lam * mean_service /60), mean service ~= 20m).
        utilization = min(1.0, st.offer_arrival_rate_per_hr * 20.0 / 60.0)
        st.expected_net_hourly = st.value_mean_per_min * 60.0 * utilization

    # ----------------------------------------------------------- retrieval
    def stats(self, hex_id: str, when: datetime,
              weather: WeatherBucket) -> ZoneStats | None:
        return self._stats.get(self._key(hex_id, when, weather))

    def _cells_for(self, when: datetime, weather: WeatherBucket) -> list[ZoneStats]:
        how = hour_of_week(when)
        return [s for (h, hw, w), s in self._stats.items()
                if hw == how and w == weather]

    # ------------------------------------------------- deadhead surface (F1)
    def return_to_density_minutes(self, from_hex: str, when: datetime,
                                  weather: WeatherBucket) -> float:
        """Expected travel minutes from a dropoff hex to the nearest hex with
        sufficient offer arrival rate for the current hour. This is
        T_return_to_density — the term nobody else models."""
        candidates = [s for s in self._cells_for(when, weather)
                      if s.offer_arrival_rate_per_hr >= self.min_arrival_rate_per_hr]
        if not candidates:
            return 0.0   # no basis; F1's default heuristic applies upstream
        best = min(self.hex.distance_miles(from_hex, s.hex_id) for s in candidates)
        return best / self.avg_speed_mph * 60.0

    # -------------------------------------------------- positioning advice
    def positioning_advice(self, current_hex: str, when: datetime,
                           weather: WeatherBucket,
                           min_gain_per_hr: float = 3.0) -> PositioningAdvice | None:
        here = self.stats(current_hex, when, weather)
        here_hourly = here.expected_net_hourly if here else 0.0
        best: PositioningAdvice | None = None
        for s in self._cells_for(when, weather):
            if s.hex_id == current_hex or s.sample_n < 3:
                continue
            gain = s.expected_net_hourly - here_hourly
            if gain < min_gain_per_hr:
                continue
            dist = self.hex.distance_miles(current_hex, s.hex_id)
            drive_min = dist / self.avg_speed_mph * 60.0
            # Break-even: the move pays only if remaining session time exceeds
            # drive time plus the time needed for the gain to repay the drive
            # cost at the destination's rate: t* = drive * dest / gain.
            if gain <= 0:
                continue
            break_even = drive_min + drive_min * (s.expected_net_hourly / gain)
            adv = PositioningAdvice(s.hex_id, dist, drive_min, gain, break_even)
            if best is None or adv.expected_gain_per_hr > best.expected_gain_per_hr:
                best = adv
        return best

    # --------------------------------------------------------- seam finder
    def find_seams(self, when: datetime, weather: WeatherBucket,
                   volume_quantile: float = 0.75,
                   value_quantile: float = 0.75) -> list[Seam]:
        cells = [s for s in self._cells_for(when, weather) if s.sample_n >= 3]
        if len(cells) < 2:
            return []
        vols = sorted(s.offer_arrival_rate_per_hr for s in cells)
        vals = sorted(s.expected_net_hourly for s in cells)
        vol_cut = vols[min(len(vols) - 1, int(volume_quantile * len(vols)))]
        val_cut = vals[min(len(vals) - 1, int(value_quantile * len(vals)))]
        high_vol = {s.hex_id: s for s in cells if s.offer_arrival_rate_per_hr >= vol_cut}
        high_val = {s.hex_id: s for s in cells if s.expected_net_hourly >= val_cut}
        seams: list[Seam] = []
        for vh, vs in high_vol.items():
            for nb in self.hex.neighbors(vh):
                ns = high_val.get(nb)
                if ns is None:
                    continue
                # A seam needs the two sides to differ on the axis that makes
                # each worth sitting between. Excluding neighbors that also
                # clear the volume cut is wrong -- the cuts overlap constantly
                # -- so compare the cells directly instead: the neighbor must
                # actually pay better, and this cell must actually see more flow.
                if ns.expected_net_hourly <= vs.expected_net_hourly:
                    continue
                if ns.offer_arrival_rate_per_hr >= vs.offer_arrival_rate_per_hr:
                    continue
                seams.append(Seam(vh, nb,
                                  vs.offer_arrival_rate_per_hr,
                                  ns.expected_net_hourly))
        return seams
