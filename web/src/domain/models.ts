/**
 * Shared vocabulary for the BLACKTOP decision engine.
 *
 * This module is pure: no DOM, no network, no storage. It takes an offer that
 * has already been read off a screenshot and returns an advisory value object.
 * Nothing here can act on the driver's behalf — the human makes every tap
 * (compliance invariants I3/I4).
 *
 * Money is dollars, time is minutes, distance is miles.
 */

export type Platform = "doordash" | "uber_eats" | "grubhub" | "manual";

export type OfferType = "single" | "stacked" | "shop_deliver" | "large_order";

/** One glance, one colour (PRD §9.1). */
export type VerdictColor = "green" | "amber" | "red" | "manual_fallback";

export type WeatherBucket = "clear" | "rain" | "snow" | "severe";

export type DestinationClass =
  | "single_family" | "multi_unit" | "high_rise"
  | "commercial" | "campus" | "hotel" | "unknown";

/** 0..167, 0 = Monday 00:00 local. */
export function hourOfWeek(d: Date): number {
  // JS getDay() is 0 = Sunday; shift so Monday is 0.
  const day = (d.getDay() + 6) % 7;
  return day * 24 + d.getHours();
}

/**
 * An offer card as read from the driver's own screenshot.
 *
 * Fields the card did not show are `null`, never `0`. The difference between
 * "this delivery has no pickup wait" and "the card never mentions pickup wait"
 * is the entire premise of the product, and defaulting to zero erases it.
 */
export interface Offer {
  offerId: string;
  platform: Platform;
  seenAt: Date;
  displayedPayout: number;
  merchantName: string;
  merchantId: string | null;
  merchantAddress: string;
  dropoffAddress: string;
  dropoffHex: string | null;
  dropoffClass: DestinationClass;
  statedDistanceMi: number | null;
  statedMinutes: number | null;
  offerType: OfferType;
  peakPay: number;
  /** The strongest single feature in the tip model, and it costs only counting. */
  hitDisplayCap: boolean;
  itemCount: number | null;
  subtotal: number | null;
}

export function makeOffer(partial: Partial<Offer> & {
  offerId: string; seenAt: Date; displayedPayout: number;
}): Offer {
  return {
    platform: "doordash",
    merchantName: "",
    merchantId: null,
    merchantAddress: "",
    dropoffAddress: "",
    dropoffHex: null,
    dropoffClass: "unknown",
    statedDistanceMi: null,
    statedMinutes: null,
    offerType: "single",
    peakPay: 0,
    hitDisplayCap: false,
    itemCount: null,
    subtotal: null,
    ...partial,
  };
}

/** The five-term time model of F1. */
export interface TimeBreakdown {
  driveToMerchantMin: number;
  merchantWaitMin: number;
  driveToCustomerMin: number;
  dropoffFrictionMin: number;
  /** The deadhead back to density. No platform surfaces it. */
  returnToDensityMin: number;
}

export function totalMinutes(t: TimeBreakdown): number {
  return t.driveToMerchantMin + t.merchantWaitMin + t.driveToCustomerMin +
    t.dropoffFrictionMin + t.returnToDensityMin;
}

/** The advisory output. Rendered and spoken; never consumed by anything that acts. */
export interface Verdict {
  color: VerdictColor;
  projectedNetHourly: number;
  reservationRateHourly: number;
  expectedPayout: number;
  expectedHiddenTip: number;
  timeBreakdown: TimeBreakdown | null;
  extractionConfidence: number;
  ttsText: string;
}

export const isActionable = (v: Verdict): boolean => v.color !== "manual_fallback";

/**
 * PRD §9.5: never show a number without what it is compared against.
 * "$38/hr" is not a decision; "$38 vs $31" is.
 */
export const headline = (v: Verdict): string =>
  `${Math.round(v.projectedNetHourly)} vs ${Math.round(v.reservationRateHourly)}`;
