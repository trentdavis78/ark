/**
 * Tests for the vision-to-engine boundary.
 *
 * This is where a hallucinated or missing field would turn into a confident
 * wrong verdict, so the conversion is pinned hard: no payout means no offer,
 * and a field the card never showed must stay null rather than becoming zero.
 */

import { describe, expect, it } from "vitest";
import { minutesUntilClockTime, toOffer } from "./client";
import type { ExtractedOffer } from "./schema";
import { ExtractedOfferSchema } from "./schema";

const NOW = new Date("2026-08-18T18:00:00");

const extracted = (over: Partial<ExtractedOffer> = {}): ExtractedOffer => ({
  displayedPayout: 9.75,
  merchantName: "Wendy's — Rt 17",
  merchantAddress: null,
  dropoffAddress: "12 Elm St, Morristown",
  statedDistanceMi: 4.2,
  statedMinutes: 23,
  deliverByClockTime: null,
  offerType: "single",
  platform: "doordash",
  peakPay: null,
  itemCount: null,
  totalMayBeHigher: false,
  isOfferCard: true,
  extractionConfidence: 0.95,
  notes: null,
  ...over,
});

describe("schema", () => {
  it("accepts a well-formed extraction", () => {
    expect(ExtractedOfferSchema.safeParse(extracted()).success).toBe(true);
  });

  it("rejects a confidence outside 0..1", () => {
    expect(ExtractedOfferSchema.safeParse(extracted({ extractionConfidence: 1.4 })).success)
      .toBe(false);
  });

  it("allows every card field to be absent", () => {
    const blank = extracted({
      displayedPayout: null, merchantName: null, statedDistanceMi: null,
      statedMinutes: null, isOfferCard: false, extractionConfidence: 0,
    });
    expect(ExtractedOfferSchema.safeParse(blank).success).toBe(true);
  });
});

describe("minutesUntilClockTime", () => {
  it("reads a 12-hour deadline later today", () => {
    expect(minutesUntilClockTime("6:42 PM", NOW)).toBeCloseTo(42);
  });

  it("rolls a passed deadline to tomorrow", () => {
    // 9:00 AM is behind 6:00 PM, so it must mean tomorrow morning.
    expect(minutesUntilClockTime("9:00 AM", NOW)).toBeCloseTo(15 * 60);
  });

  it("handles noon and midnight", () => {
    expect(minutesUntilClockTime("12:30 AM", NOW)).toBeCloseTo(6.5 * 60);
    expect(minutesUntilClockTime("12:30 PM", NOW)).toBeCloseTo(18.5 * 60);
  });

  it("reads 24-hour time", () => {
    expect(minutesUntilClockTime("18:42", NOW)).toBeCloseTo(42);
  });

  it("rejects nonsense rather than guessing", () => {
    expect(minutesUntilClockTime("soon", NOW)).toBeNull();
    expect(minutesUntilClockTime("25:99", NOW)).toBeNull();
    expect(minutesUntilClockTime("", NOW)).toBeNull();
  });
});

describe("toOffer", () => {
  it("converts a complete extraction", () => {
    const o = toOffer(extracted(), "o1", NOW);
    expect(o).not.toBeNull();
    expect(o!.displayedPayout).toBe(9.75);
    expect(o!.statedDistanceMi).toBe(4.2);
    expect(o!.statedMinutes).toBe(23);
    expect(o!.platform).toBe("doordash");
  });

  it("refuses to build an offer with no payout", () => {
    // There is nothing to decide without it, and a zero would look real.
    expect(toOffer(extracted({ displayedPayout: null }), "o1", NOW)).toBeNull();
  });

  it("refuses a screenshot that is not an offer card", () => {
    expect(toOffer(extracted({ isOfferCard: false }), "o1", NOW)).toBeNull();
  });

  it("keeps a missing distance null instead of zero", () => {
    const o = toOffer(extracted({ statedDistanceMi: null }), "o1", NOW);
    expect(o!.statedDistanceMi).toBeNull();
  });

  it("derives minutes from a deliver-by time when no duration is shown", () => {
    const o = toOffer(
      extracted({ statedMinutes: null, deliverByClockTime: "6:35 PM" }), "o1", NOW);
    expect(o!.statedMinutes).toBeCloseTo(35);
  });

  it("leaves minutes null when neither duration nor deadline is readable", () => {
    const o = toOffer(
      extracted({ statedMinutes: null, deliverByClockTime: "whenever" }), "o1", NOW);
    expect(o!.statedMinutes).toBeNull();
  });

  it("marks the cap from the card's own disclaimer", () => {
    expect(toOffer(extracted({ totalMayBeHigher: true }), "o1", NOW)!.hitDisplayCap)
      .toBe(true);
  });

  it("marks the cap from a learned market ceiling", () => {
    const o = toOffer(extracted({ displayedPayout: 12 }), "o1", NOW, 12);
    expect(o!.hitDisplayCap).toBe(true);
  });

  it("does not mark the cap for an unrelated amount", () => {
    expect(toOffer(extracted({ displayedPayout: 8.5 }), "o1", NOW, 12)!.hitDisplayCap)
      .toBe(false);
  });

  it("falls back to doordash when the platform is unrecognised", () => {
    expect(toOffer(extracted({ platform: "unknown" }), "o1", NOW)!.platform)
      .toBe("doordash");
  });

  it("defaults a missing peak pay to zero", () => {
    expect(toOffer(extracted({ peakPay: null }), "o1", NOW)!.peakPay).toBe(0);
  });
});
