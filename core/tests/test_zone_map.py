from datetime import datetime

import pytest

from blacktop.models import WeatherBucket
from blacktop.zone_map import GridHexIndex, ZoneMap

NOW = datetime(2026, 8, 18, 18, 5)
W = WeatherBucket.CLEAR


class TestGridHexIndex:
    def test_same_point_same_cell(self):
        h = GridHexIndex()
        assert h.cell(40.79, -74.48) == h.cell(40.79, -74.48)

    def test_distant_points_different_cells(self):
        h = GridHexIndex()
        assert h.cell(40.79, -74.48) != h.cell(40.90, -74.10)

    def test_neighbors_are_eight_and_adjacent(self):
        h = GridHexIndex()
        c = h.cell(40.79, -74.48)
        nbs = h.neighbors(c)
        assert len(nbs) == 8
        assert c not in nbs
        assert all(h.distance_miles(c, n) <= h.cell_miles * 1.5 for n in nbs)

    def test_distance_scales_with_cells(self):
        h = GridHexIndex(cell_miles=0.5)
        assert h.distance_miles("g8:0:0", "g8:0:4") == pytest.approx(2.0)
        assert h.distance_miles("g8:3:4", "g8:3:4") == 0.0

    def test_center_roundtrip(self):
        h = GridHexIndex()
        c = h.cell(40.79, -74.48)
        lat, lng = h.center(c)
        assert h.cell(lat, lng) == c


class TestZoneStatsLearning:
    def test_arrival_rate_needs_presence(self):
        zm = ZoneMap()
        zm.record_offer("h1", NOW, W, 0.5)
        st = zm.stats("h1", NOW, W)
        assert st.offer_arrival_rate_per_hr == 0.0   # no observed hours yet

    def test_arrival_rate_computed(self):
        zm = ZoneMap()
        zm.record_presence("h1", NOW, W, hours=2.0)
        for _ in range(10):
            zm.record_offer("h1", NOW, W, 0.5)
        assert zm.stats("h1", NOW, W).offer_arrival_rate_per_hr == pytest.approx(5.0)

    def test_running_mean(self):
        zm = ZoneMap()
        zm.record_presence("h1", NOW, W, 1.0)
        for v in (0.4, 0.6, 0.8):
            zm.record_offer("h1", NOW, W, v)
        assert zm.stats("h1", NOW, W).value_mean_per_min == pytest.approx(0.6)

    def test_buckets_are_independent(self):
        zm = ZoneMap()
        zm.record_offer("h1", NOW, W, 0.5)
        assert zm.stats("h1", NOW, WeatherBucket.SNOW) is None
        other_hour = NOW.replace(hour=9)
        assert zm.stats("h1", other_hour, W) is None

    def test_quality_not_volume(self):
        """The Paramus-vs-Morristown case: high volume mediocre value vs.
        low volume high value — expected hourly must rank by quality."""
        zm = ZoneMap()
        zm.record_presence("paramus", NOW, W, 2.0)
        zm.record_presence("morristown", NOW, W, 2.0)
        for _ in range(24):
            zm.record_offer("paramus", NOW, W, 0.35)     # 12/hr of mediocre
        for _ in range(8):
            zm.record_offer("morristown", NOW, W, 0.90)  # 4/hr of great
        p = zm.stats("paramus", NOW, W)
        m = zm.stats("morristown", NOW, W)
        assert p.offer_arrival_rate_per_hr > m.offer_arrival_rate_per_hr
        assert m.expected_net_hourly > p.expected_net_hourly


class TestDeadheadSurface:
    def build(self):
        zm = ZoneMap()
        zm.record_presence("g8:100:100", NOW, W, 2.0)
        for _ in range(12):
            zm.record_offer("g8:100:100", NOW, W, 0.6)   # 6/hr, dense enough
        return zm

    def test_zero_when_no_data(self):
        assert ZoneMap().return_to_density_minutes("g8:0:0", NOW, W) == 0.0

    def test_deadhead_minutes_scale_with_distance(self):
        zm = self.build()
        near = zm.return_to_density_minutes("g8:100:101", NOW, W)
        far = zm.return_to_density_minutes("g8:100:120", NOW, W)
        assert 0 < near < far

    def test_inside_dense_hex_is_zero(self):
        zm = self.build()
        assert zm.return_to_density_minutes("g8:100:100", NOW, W) == 0.0

    def test_sparse_hexes_not_counted_as_density(self):
        zm = ZoneMap(min_arrival_rate_per_hr=4.0)
        zm.record_presence("g8:100:100", NOW, W, 2.0)
        for _ in range(2):                               # only 1/hr
            zm.record_offer("g8:100:100", NOW, W, 0.6)
        assert zm.return_to_density_minutes("g8:100:110", NOW, W) == 0.0


class TestPositioning:
    def build(self):
        zm = ZoneMap()
        zm.record_presence("g8:100:100", NOW, W, 2.0)
        zm.record_presence("g8:100:104", NOW, W, 2.0)
        for _ in range(10):
            zm.record_offer("g8:100:100", NOW, W, 0.40)
        for _ in range(10):
            zm.record_offer("g8:100:104", NOW, W, 0.95)
        return zm

    def test_recommends_better_hex(self):
        adv = self.build().positioning_advice("g8:100:100", NOW, W)
        assert adv is not None
        assert adv.target_hex == "g8:100:104"
        assert adv.expected_gain_per_hr > 0
        assert adv.distance_miles == pytest.approx(4 * 0.46)
        assert adv.drive_cost_minutes > 0
        assert adv.break_even_minutes > adv.drive_cost_minutes

    def test_render_matches_prd_shape(self):
        text = self.build().positioning_advice("g8:100:100", NOW, W).render()
        assert "Move" in text and "Expected +$" in text and "Break-even" in text

    def test_no_advice_when_already_best(self):
        assert self.build().positioning_advice("g8:100:104", NOW, W) is None

    def test_min_gain_filter(self):
        adv = self.build().positioning_advice("g8:100:100", NOW, W,
                                              min_gain_per_hr=1000.0)
        assert adv is None

    def test_thin_cells_ignored(self):
        zm = ZoneMap()
        zm.record_presence("a", NOW, W, 1.0)
        zm.record_offer("g8:100:104", NOW, W, 5.0)   # n=1 < 3
        assert zm.positioning_advice("g8:100:100", NOW, W) is None


class TestSeams:
    def test_finds_adjacent_volume_value_pair(self):
        zm = ZoneMap()
        for hexid, val, n in (("g8:10:10", 0.35, 20), ("g8:10:11", 0.95, 6),
                              ("g8:50:50", 0.40, 5)):
            zm.record_presence(hexid, NOW, W, 1.0)
            for _ in range(n):
                zm.record_offer(hexid, NOW, W, val)
        seams = zm.find_seams(NOW, W, volume_quantile=0.6, value_quantile=0.6)
        assert any(s.volume_hex == "g8:10:10" and s.value_hex == "g8:10:11"
                   for s in seams)

    def test_empty_map_no_seams(self):
        assert ZoneMap().find_seams(NOW, W) == []
