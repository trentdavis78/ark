/**
 * F1 — Offer Verdict Engine. The product; everything else supports it.
 *
 * Extracted offer → five-term time model → value model (F2) → compare against
 * the dynamic reservation rate (F6) → Verdict.
 *
 * Every external signal is injected, so the engine is deterministic, testable
 * without a browser, and usable offline. Where a provider is absent the
 * fallback is a documented heuristic, never a silent zero.
 */

import type { Offer, TimeBreakdown, Verdict } from "./models";
import { totalMinutes } from "./models";
import type { RateDecision } from "./reservation";
import { completionRisk } from "./reservation";

/** Below this we refuse to rule and hand the card back to the driver. */
export const MIN_EXTRACTION_CONFIDENCE = 0.75;

/** Within ±8% of the threshold the honest answer is "judgment call". */
export const AMBER_BAND = 0.08;

const STATED_MINUTES_TO_MERCHANT_SHARE = 0.4;

export interface VerdictConfig {
  minExtractionConfidence: number;
  amberBand: number;
  avgSpeedMph: number;
  /** Platforms understate drive time; inflate what the card claims. */
  trafficInflation: number;
  defaultMerchantWaitMin: number;
  defaultDropoffFrictionMin: number;
  defaultReturnToDensityMin: number;
}

export const DEFAULT_VERDICT_CONFIG: VerdictConfig = {
  minExtractionConfidence: MIN_EXTRACTION_CONFIDENCE,
  amberBand: AMBER_BAND,
  avgSpeedMph: 25,
  trafficInflation: 1.1,
  defaultMerchantWaitMin: 5,
  defaultDropoffFrictionMin: 2,
  defaultReturnToDensityMin: 4,
};

/** Cached routing. Returning null means "no estimate", not "zero minutes". */
export interface RouteProvider {
  driveToMerchantMinutes(offer: Offer): number | null;
  driveToCustomerMinutes(offer: Offer): number | null;
}

export interface VerdictProviders {
  tipEstimator?: (offer: Offer) => number;
  merchantWait?: (offer: Offer, now: Date) => number;
  dropoffFriction?: (offer: Offer) => number;
  returnToDensity?: (offer: Offer, now: Date) => number;
  routeProvider?: RouteProvider;
  config?: Partial<VerdictConfig>;
}

export class VerdictEngine {
  readonly config: VerdictConfig;

  constructor(private providers: VerdictProviders = {}) {
    this.config = { ...DEFAULT_VERDICT_CONFIG, ...providers.config };
  }

  buildTimeBreakdown(offer: Offer, now: Date): TimeBreakdown {
    const rp = this.providers.routeProvider;
    let toMerchant = rp ? rp.driveToMerchantMinutes(offer) : null;
    let toCustomer = rp ? rp.driveToCustomerMinutes(offer) : null;
    if (toMerchant === null || toCustomer === null) {
      const [estM, estC] = this.heuristicDriveMinutes(offer);
      toMerchant ??= estM;
      toCustomer ??= estC;
    }
    return {
      driveToMerchantMin: toMerchant,
      merchantWaitMin:
        this.providers.merchantWait?.(offer, now) ?? this.config.defaultMerchantWaitMin,
      driveToCustomerMin: toCustomer,
      dropoffFrictionMin:
        this.providers.dropoffFriction?.(offer) ?? this.config.defaultDropoffFrictionMin,
      returnToDensityMin:
        this.providers.returnToDensity?.(offer, now) ??
        this.config.defaultReturnToDensityMin,
    };
  }

  /** Fallback when routing is unavailable: split the card's own (inflated)
   *  estimate, or derive one from stated distance at a blended speed. */
  private heuristicDriveMinutes(offer: Offer): [number, number] {
    const { statedMinutes, statedDistanceMi } = offer;
    let total: number;
    if (statedMinutes !== null && statedMinutes > 0) {
      total = statedMinutes * this.config.trafficInflation;
    } else if (statedDistanceMi !== null && statedDistanceMi > 0) {
      total =
        (statedDistanceMi / this.config.avgSpeedMph) * 60 * this.config.trafficInflation;
    } else {
      total = 15; // nothing to go on; deliberately conservative
    }
    return [
      total * STATED_MINUTES_TO_MERCHANT_SHARE,
      total * (1 - STATED_MINUTES_TO_MERCHANT_SHARE),
    ];
  }

  /** Returns expected total payout and the hidden-tip component of it. */
  expectedPayout(offer: Offer): [number, number] {
    const tip = Math.max(0, this.providers.tipEstimator?.(offer) ?? 0);
    return [offer.displayedPayout + tip, tip];
  }

  evaluate(
    offer: Offer,
    now: Date,
    reservation: RateDecision,
    extractionConfidence = 1,
    remainingSessionMinutes: number | null = null,
  ): Verdict {
    // A card we could not read is the driver's call, not ours. Emitting a
    // confident-looking number from a bad extraction is the one failure mode
    // that would actively cost them money (PRD §11).
    if (extractionConfidence < this.config.minExtractionConfidence) {
      return {
        color: "manual_fallback",
        projectedNetHourly: 0,
        reservationRateHourly: reservation.hourly,
        expectedPayout: offer.displayedPayout,
        expectedHiddenTip: 0,
        timeBreakdown: null,
        extractionConfidence,
        ttsText: "Couldn't read that one. Your call.",
      };
    }

    const tb = this.buildTimeBreakdown(offer, now);
    const [expected, tip] = this.expectedPayout(offer);
    const total = Math.max(totalMinutes(tb), 1e-9);
    const hourly = (expected / total) * 60;
    const threshold = reservation.hourly;

    let color: Verdict["color"];
    let ttsText: string;
    if (completionRisk(total, remainingSessionMinutes)) {
      color = "red";
      ttsText = "Skip it. Won't finish before you're done.";
    } else if (hourly >= threshold * (1 + this.config.amberBand)) {
      color = "green";
      ttsText = `Take it. ${Math.round(hourly)}.`;
    } else if (hourly <= threshold * (1 - this.config.amberBand)) {
      color = "red";
      ttsText = `Skip it. ${Math.round(hourly)} against ${Math.round(threshold)}.`;
    } else {
      color = "amber";
      ttsText = `Close call. ${Math.round(hourly)} against ${Math.round(threshold)}.`;
    }

    return {
      color,
      projectedNetHourly: hourly,
      reservationRateHourly: threshold,
      expectedPayout: expected,
      expectedHiddenTip: tip,
      timeBreakdown: tb,
      extractionConfidence,
      ttsText,
    };
  }
}
