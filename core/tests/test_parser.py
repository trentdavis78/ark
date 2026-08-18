from datetime import datetime

import pytest

from blacktop.models import OfferType, Platform
from blacktop.parser import manual_offer, parse_offer_text
from blacktop.verdict import MIN_PARSE_CONFIDENCE

NOW = datetime(2026, 8, 18, 18, 0)

DD_CARD = """$9.75
Total may be higher
Chipotle Mexican Grill
2.3 mi
25 min
Accept
Decline"""

UE_CARD = """$12.50 Guaranteed (incl. tips)
McDonald's
4.1 mi
18 min"""

STACKED_CARD = """$14.25
2 orders
Wendy's
5.0 mi
Deliver by 6:45 PM"""

SHOP_CARD = """$18.00
Shop for items
ShopRite of Morris Plains
12 items
3.2 mi
40 min"""

PEAK_CARD = """$11.00
+$2.00 Peak Pay
Five Guys
1.9 mi
15 min"""


class TestHappyPath:
    def test_doordash_card(self):
        p = parse_offer_text(DD_CARD, Platform.DOORDASH, NOW)
        assert p.offer.displayed_payout == 9.75
        assert p.offer.merchant_name == "Chipotle Mexican Grill"
        assert p.offer.stated_distance_mi == 2.3
        assert p.offer.stated_minutes == 25.0
        assert p.offer.hit_display_cap is True     # "Total may be higher"
        assert p.confidence == 1.0
        assert p.failures == ()

    def test_uber_card(self):
        p = parse_offer_text(UE_CARD, Platform.UBER_EATS, NOW)
        assert p.offer.displayed_payout == 12.50
        assert p.offer.merchant_name == "McDonald's"
        assert not p.offer.hit_display_cap

    def test_stacked_and_deliver_by(self):
        p = parse_offer_text(STACKED_CARD, Platform.DOORDASH, NOW)
        assert p.offer.offer_type is OfferType.STACKED
        assert p.offer.stated_minutes == pytest.approx(45.0)   # 6:45pm from 6:00pm

    def test_shop_and_deliver_with_items(self):
        p = parse_offer_text(SHOP_CARD, Platform.DOORDASH, NOW)
        assert p.offer.offer_type is OfferType.SHOP_DELIVER
        assert p.offer.item_count == 12
        assert p.offer.merchant_name == "ShopRite of Morris Plains"

    def test_peak_pay_extracted_not_double_counted(self):
        p = parse_offer_text(PEAK_CARD, Platform.DOORDASH, NOW)
        assert p.offer.displayed_payout == 11.00   # first non-peak figure
        assert p.offer.peak_pay == 2.00

    def test_plus_suffix_marks_cap(self):
        p = parse_offer_text("$9.75+\nBurger King\n2 mi\n15 min",
                             Platform.DOORDASH, NOW)
        assert p.offer.hit_display_cap

    def test_known_cap_value_marks_cap(self):
        p = parse_offer_text("$9.75\nBurger King\n2 mi\n15 min",
                             Platform.DOORDASH, NOW, known_cap_value=9.75)
        assert p.offer.hit_display_cap
        p2 = parse_offer_text("$8.75\nBurger King\n2 mi\n15 min",
                              Platform.DOORDASH, NOW, known_cap_value=9.75)
        assert not p2.offer.hit_display_cap

    def test_large_order(self):
        p = parse_offer_text("$32.00\nLarge Order\nCosta's Catering\n6 mi\n35 min",
                             Platform.DOORDASH, NOW)
        assert p.offer.offer_type is OfferType.LARGE_ORDER


class TestDegradation:
    def test_missing_payout_is_fatal_confidence(self):
        p = parse_offer_text("Chipotle\n2.3 mi\n25 min", Platform.DOORDASH, NOW)
        assert "payout_not_found" in p.failures
        assert p.confidence < MIN_PARSE_CONFIDENCE   # F1 will refuse a verdict

    def test_garbage_below_gate(self):
        p = parse_offer_text("■■■ ??? ***", Platform.DOORDASH, NOW)
        assert p.confidence < MIN_PARSE_CONFIDENCE

    def test_partial_card_still_reports_fields(self):
        p = parse_offer_text("$7.25\nTaco Bell", Platform.DOORDASH, NOW)
        assert p.offer.displayed_payout == 7.25
        assert set(p.failures) == {"distance_not_found", "time_not_found"}
        assert 0 < p.confidence < 1.0

    def test_failures_are_telemetry_not_exceptions(self):
        # Any input must return a ParsedOffer, never raise (PRD 7: parse
        # failures are telemetry, not silent errors).
        for text in ("", "\n\n", "$", "mi min", "Accept\nDecline"):
            p = parse_offer_text(text, Platform.DOORDASH, NOW)
            assert 0.0 <= p.confidence <= 1.0

    def test_noise_lines_do_not_become_merchant(self):
        p = parse_offer_text("$8.00\nAccept\nDecline\n6:45\n2 mi\n20 min",
                             Platform.DOORDASH, NOW)
        assert p.offer.merchant_name == ""

    def test_deliver_by_past_midnight_wraps(self):
        late = datetime(2026, 8, 18, 23, 30)
        p = parse_offer_text("$8.00\nDiner\n2 mi\nDeliver by 12:10 AM",
                             Platform.DOORDASH, late)
        assert p.offer.stated_minutes == pytest.approx(40.0)


class TestManualMode:
    """I6 — manual entry is a first-class path."""

    def test_full_confidence(self):
        p = manual_offer(7.5, NOW, distance_mi=3.0, minutes=20.0,
                         merchant_name="Chipotle")
        assert p.confidence == 1.0
        assert p.source == "manual"
        assert p.failures == ()
        assert p.offer.platform is Platform.MANUAL

    def test_minimal_entry_works(self):
        p = manual_offer(6.0, NOW)
        assert p.confidence == 1.0    # standalone: payout alone is enough

    def test_negative_payout_rejected(self):
        with pytest.raises(ValueError):
            manual_offer(-1.0, NOW)

    def test_same_type_as_ocr_path(self):
        ocr = parse_offer_text(DD_CARD, Platform.DOORDASH, NOW)
        man = manual_offer(9.75, NOW)
        assert type(ocr) is type(man)
        assert type(ocr.offer) is type(man.offer)
