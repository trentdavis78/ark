"""Offer-card text -> structured ``Offer`` (F1 extraction stage).

Input is OCR text (ML Kit) or concatenated accessibility-node text from the
driver's own screen — never a platform API payload (I2). Layouts change
without notice (PRD 11), so this parser:

* extracts each field independently with tolerant patterns,
* scores parse confidence per field and overall,
* returns machine-readable failure reasons (parse failures are telemetry,
  not silent errors),
* and never guesses: a field it cannot find is None, and a low overall
  confidence tells F1 to emit MANUAL_FALLBACK instead of a verdict.

``manual_offer`` is the I6 first-class manual-entry path: structured input,
confidence 1.0, same ``ParsedOffer`` type.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .models import Offer, OfferType, Platform

# Weights for overall confidence. Payout is the field a verdict cannot live
# without; the others degrade gracefully to heuristics.
FIELD_WEIGHTS = {"payout": 0.45, "distance": 0.15, "time": 0.15, "merchant": 0.25}

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
_RE_PAYOUT = re.compile(_MONEY + r"\s*(\+|\bplus\b)?", re.I)
_RE_GUARANTEE = re.compile(r"(guaranteed|incl(?:udes|\.)?\s+tips?)", re.I)
_RE_MAY_BE_HIGHER = re.compile(r"total\s+(?:may|will)\s+be\s+higher", re.I)
_RE_DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*mi\b", re.I)
_RE_MINUTES = re.compile(r"(\d+)\s*min\b", re.I)
_RE_DELIVER_BY = re.compile(r"deliver\s+by\s+(\d{1,2}):(\d{2})\s*(am|pm)?", re.I)
_RE_PEAK = re.compile(r"\+\s?" + _MONEY + r"\s*peak\s*pay|peak\s*pay\s*\+?\s?" + _MONEY, re.I)
_RE_STACKED = re.compile(r"(\b2\s+orders?\b|\b\d\s+orders\b|stacked|batched)", re.I)
_RE_SHOP = re.compile(r"(shop\s*(&|and)?\s*deliver|shop\s+for\s+items)", re.I)
_RE_LARGE = re.compile(r"large\s+order", re.I)
_RE_ITEMS = re.compile(r"(\d+)\s*items?\b", re.I)
# Junk lines OCR commonly produces around the card.
_RE_NOISE = re.compile(
    r"^(accept|decline|reject|\d{1,2}:\d{2}|●+|▸+|[|_\-—=~]{2,}|now|new)$", re.I)


@dataclass(frozen=True)
class ParsedOffer:
    """Extraction result: the offer, per-field confidence, telemetry."""
    offer: Offer
    confidence: float
    field_confidence: dict[str, float] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    source: str = "ocr"       # "ocr" | "accessibility" | "manual"


def _clean_lines(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not _RE_NOISE.match(ln)]


def _extract_payout(text: str) -> tuple[float | None, bool]:
    """Returns (payout, may_be_higher). The payout is the FIRST prominent
    dollar figure that isn't a Peak Pay line item; cards lead with it."""
    may_higher = bool(_RE_MAY_BE_HIGHER.search(text))
    peak_spans = [m.span() for m in _RE_PEAK.finditer(text)]
    for m in _RE_PAYOUT.finditer(text):
        if any(s <= m.start() < e for s, e in peak_spans):
            continue
        value = float(m.group(1).replace(",", ""))
        if m.group(2):   # "$9.75+" style
            may_higher = True
        return value, may_higher
    return None, may_higher


def _extract_merchant(lines: list[str]) -> str | None:
    """The merchant name is the first line that isn't money/distance/time
    boilerplate."""
    for ln in lines:
        if _RE_PAYOUT.search(ln) or _RE_DISTANCE.search(ln) or _RE_MINUTES.search(ln):
            continue
        if _RE_DELIVER_BY.search(ln) or _RE_MAY_BE_HIGHER.search(ln) or _RE_GUARANTEE.search(ln):
            continue
        if _RE_SHOP.search(ln) or _RE_LARGE.search(ln) or _RE_STACKED.search(ln):
            continue
        if len(ln) < 2:
            continue
        return ln
    return None


def _extract_minutes(text: str, now: datetime) -> float | None:
    m = _RE_MINUTES.search(text)
    if m:
        return float(m.group(1))
    m = _RE_DELIVER_BY.search(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hh != 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        deadline = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0)
        delta = (deadline - now).total_seconds() / 60.0
        if delta < 0:
            delta += 24 * 60.0
        return delta if 0 < delta <= 120 else None
    return None


def _offer_type(text: str) -> OfferType:
    if _RE_SHOP.search(text):
        return OfferType.SHOP_DELIVER
    if _RE_LARGE.search(text):
        return OfferType.LARGE_ORDER
    if _RE_STACKED.search(text):
        return OfferType.STACKED
    return OfferType.SINGLE


def parse_offer_text(text: str,
                     platform: Platform,
                     now: datetime,
                     source: str = "ocr",
                     known_cap_value: float | None = None) -> ParsedOffer:
    """Parse one offer card's worth of screen text."""
    lines = _clean_lines(text)
    joined = "\n".join(lines)
    failures: list[str] = []
    fc: dict[str, float] = {}

    payout, may_higher = _extract_payout(joined)
    if payout is None:
        failures.append("payout_not_found")
        fc["payout"] = 0.0
        payout_val = 0.0
    else:
        fc["payout"] = 1.0
        payout_val = payout

    dist = None
    m = _RE_DISTANCE.search(joined)
    if m:
        dist = float(m.group(1))
        fc["distance"] = 1.0
    else:
        failures.append("distance_not_found")
        fc["distance"] = 0.0

    minutes = _extract_minutes(joined, now)
    fc["time"] = 1.0 if minutes is not None else 0.0
    if minutes is None:
        failures.append("time_not_found")

    merchant = _extract_merchant(lines)
    fc["merchant"] = 1.0 if merchant else 0.0
    if not merchant:
        failures.append("merchant_not_found")

    peak = 0.0
    pm = _RE_PEAK.search(joined)
    if pm:
        peak = float((pm.group(1) or pm.group(2)).replace(",", ""))

    items = None
    im = _RE_ITEMS.search(joined)
    if im:
        items = int(im.group(1))

    hit_cap = bool(may_higher)
    if known_cap_value is not None and payout is not None:
        hit_cap = hit_cap or abs(payout - known_cap_value) < 0.005

    confidence = sum(FIELD_WEIGHTS[k] * fc[k] for k in FIELD_WEIGHTS)

    offer = Offer(
        offer_id=str(uuid.uuid4()),
        platform=platform,
        seen_at=now,
        displayed_payout=payout_val,
        merchant_name=merchant or "",
        stated_distance_mi=dist,
        stated_minutes=minutes,
        offer_type=_offer_type(joined),
        peak_pay=peak,
        hit_display_cap=hit_cap,
        item_count=items,
    )
    return ParsedOffer(offer=offer, confidence=confidence,
                       field_confidence=fc, failures=tuple(failures),
                       source=source)


def manual_offer(displayed_payout: float,
                 now: datetime,
                 distance_mi: float | None = None,
                 minutes: float | None = None,
                 merchant_name: str = "",
                 platform: Platform = Platform.MANUAL,
                 offer_type: OfferType = OfferType.SINGLE,
                 hit_display_cap: bool = False) -> ParsedOffer:
    """I6 — the manual-entry fallback, a first-class path. Structured input
    from the driver, confidence 1.0, works with screen reading disabled."""
    if displayed_payout < 0:
        raise ValueError("payout cannot be negative")
    offer = Offer(
        offer_id=str(uuid.uuid4()),
        platform=platform,
        seen_at=now,
        displayed_payout=displayed_payout,
        merchant_name=merchant_name,
        stated_distance_mi=distance_mi,
        stated_minutes=minutes,
        offer_type=offer_type,
        hit_display_cap=hit_display_cap,
    )
    return ParsedOffer(offer=offer, confidence=1.0,
                       field_confidence={k: 1.0 for k in FIELD_WEIGHTS},
                       failures=(), source="manual")
