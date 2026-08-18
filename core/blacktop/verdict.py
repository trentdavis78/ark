"""F1 — Offer Verdict Engine. The product; everything else supports this.

Pipeline: parsed offer -> five-term time model -> value model (F2 tip
estimate) -> compare vs. the dynamic reservation rate (F6) -> Verdict.

Compliance: the Verdict is a pure value object rendered to overlay/TTS and
nothing else (I3/I4). A low-confidence parse never produces a verdict — it
produces MANUAL_FALLBACK, pointing the driver at manual entry (I6).

All external signals are injected so the engine is deterministic and
offline-first: routing, merchant wait (F4), building friction (F5), and the
deadhead surface (F3) are provider callables with honest heuristic fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .models import Offer, TimeBreakdown, Verdict, VerdictColor
from .reservation import RateDecision, completion_risk

# Below this parse confidence we refuse to emit a verdict (PRD 11: never emit
# a verdict from a low-confidence parse; degrade to manual mode instead).
MIN_PARSE_CONFIDENCE = 0.75

# Indifference band around the reservation rate: within +-8% the honest answer
# is "judgment call", rendered AMBER.
AMBER_BAND = 0.08

# Heuristic fallbacks when no routing provider is injected.
AVG_SPEED_MPH = 25.0                 # suburban NJ blended average
STATED_MINUTES_TO_MERCHANT_SHARE = 0.40
TRAFFIC_INFLATION = 1.10             # platforms understate; inflate stated time
DEFAULT_MERCHANT_WAIT_MIN = 5.0
DEFAULT_DROPOFF_FRICTION_MIN = 2.0
DEFAULT_RETURN_TO_DENSITY_MIN = 4.0


class RouteProvider(Protocol):
    """Injected routing (cached matrix on device; deterministic in tests)."""
    def drive_to_merchant_minutes(self, offer: Offer) -> float | None: ...
    def drive_to_customer_minutes(self, offer: Offer) -> float | None: ...


@dataclass(frozen=True)
class VerdictConfig:
    min_parse_confidence: float = MIN_PARSE_CONFIDENCE
    amber_band: float = AMBER_BAND
    avg_speed_mph: float = AVG_SPEED_MPH
    traffic_inflation: float = TRAFFIC_INFLATION
    default_merchant_wait_min: float = DEFAULT_MERCHANT_WAIT_MIN
    default_dropoff_friction_min: float = DEFAULT_DROPOFF_FRICTION_MIN
    default_return_to_density_min: float = DEFAULT_RETURN_TO_DENSITY_MIN


class VerdictEngine:
    """Wires F2-F6 into the single verdict call.

    Providers (all optional; None -> documented heuristic fallback):
      tip_estimator:      offer -> expected hidden tip dollars (F2)
      merchant_wait:      (offer, now) -> minutes (F4)
      dropoff_friction:   offer -> minutes (F5)
      return_to_density:  (offer, now) -> minutes (F3 deadhead surface)
      route_provider:     RouteProvider (cached routing)
    """

    def __init__(self,
                 tip_estimator: Callable[[Offer], float] | None = None,
                 merchant_wait: Callable[[Offer, datetime], float] | None = None,
                 dropoff_friction: Callable[[Offer], float] | None = None,
                 return_to_density: Callable[[Offer, datetime], float] | None = None,
                 route_provider: RouteProvider | None = None,
                 config: VerdictConfig = VerdictConfig()) -> None:
        self.tip_estimator = tip_estimator
        self.merchant_wait = merchant_wait
        self.dropoff_friction = dropoff_friction
        self.return_to_density = return_to_density
        self.route_provider = route_provider
        self.config = config

    # ---------------------------------------------------------------- time
    def build_time_breakdown(self, offer: Offer, now: datetime) -> TimeBreakdown:
        cfg = self.config
        to_merchant = to_customer = None
        if self.route_provider is not None:
            to_merchant = self.route_provider.drive_to_merchant_minutes(offer)
            to_customer = self.route_provider.drive_to_customer_minutes(offer)
        if to_merchant is None or to_customer is None:
            est_m, est_c = self._heuristic_drive_minutes(offer)
            to_merchant = to_merchant if to_merchant is not None else est_m
            to_customer = to_customer if to_customer is not None else est_c

        wait = (self.merchant_wait(offer, now) if self.merchant_wait
                else cfg.default_merchant_wait_min)
        friction = (self.dropoff_friction(offer) if self.dropoff_friction
                    else cfg.default_dropoff_friction_min)
        deadhead = (self.return_to_density(offer, now) if self.return_to_density
                    else cfg.default_return_to_density_min)
        return TimeBreakdown(
            drive_to_merchant_min=to_merchant,
            merchant_wait_min=wait,
            drive_to_customer_min=to_customer,
            dropoff_friction_min=friction,
            return_to_density_min=deadhead,
        )

    def _heuristic_drive_minutes(self, offer: Offer) -> tuple[float, float]:
        """Fallback when routing is unavailable: split the (inflated) stated
        minutes, else derive from stated distance at blended speed."""
        cfg = self.config
        if offer.stated_minutes is not None and offer.stated_minutes > 0:
            total = offer.stated_minutes * cfg.traffic_inflation
        elif offer.stated_distance_mi is not None and offer.stated_distance_mi > 0:
            total = offer.stated_distance_mi / cfg.avg_speed_mph * 60.0 * cfg.traffic_inflation
        else:
            total = 15.0  # nothing to go on; conservative default
        return (total * STATED_MINUTES_TO_MERCHANT_SHARE,
                total * (1.0 - STATED_MINUTES_TO_MERCHANT_SHARE))

    # --------------------------------------------------------------- value
    def expected_payout(self, offer: Offer) -> tuple[float, float]:
        """(E[total payout], E[hidden tip])."""
        tip = self.tip_estimator(offer) if self.tip_estimator else 0.0
        return offer.displayed_payout + max(0.0, tip), max(0.0, tip)

    # ------------------------------------------------------------- verdict
    def evaluate(self,
                 offer: Offer,
                 now: datetime,
                 reservation: RateDecision,
                 parse_confidence: float = 1.0,
                 remaining_session_minutes: float | None = None) -> Verdict:
        cfg = self.config
        if parse_confidence < cfg.min_parse_confidence:
            return Verdict(
                color=VerdictColor.MANUAL_FALLBACK,
                projected_net_hourly=0.0,
                reservation_rate_hourly=reservation.hourly,
                expected_payout=offer.displayed_payout,
                expected_hidden_tip=0.0,
                time_breakdown=None,
                parse_confidence=parse_confidence,
                tts_text="Couldn't read that one. Check it yourself.",
            )

        tb = self.build_time_breakdown(offer, now)
        e_payout, e_tip = self.expected_payout(offer)
        total_min = max(tb.total_minutes, 1e-9)
        e_hourly = e_payout / total_min * 60.0
        threshold = reservation.hourly

        risky = completion_risk(total_min, remaining_session_minutes)
        if risky:
            color = VerdictColor.RED
            tts = "Skip it. Won't finish before you're done."
        elif e_hourly >= threshold * (1.0 + cfg.amber_band):
            color = VerdictColor.GREEN
            tts = f"Take it. {round(e_hourly)}."
        elif e_hourly <= threshold * (1.0 - cfg.amber_band):
            color = VerdictColor.RED
            tts = f"Skip it. {round(e_hourly)} against {round(threshold)}."
        else:
            color = VerdictColor.AMBER
            tts = f"Close call. {round(e_hourly)} against {round(threshold)}."

        return Verdict(
            color=color,
            projected_net_hourly=e_hourly,
            reservation_rate_hourly=threshold,
            expected_payout=e_payout,
            expected_hidden_tip=e_tip,
            time_breakdown=tb,
            parse_confidence=parse_confidence,
            tts_text=tts,
        )
