/**
 * Schema tests.
 *
 * This schema is the contract between the vision model and the Android
 * client's parser, and it is where a hallucinated field would turn into a
 * confident wrong verdict. So the properties pinned here are the safety ones:
 * every card field must be allowed to be absent, and confidence must be a
 * real number in range.
 */

import { describe, expect, it } from "vitest";
import { ExtractedOfferSchema, ExtractResponseSchema } from "./schema";

const valid = {
  displayedPayout: 9.75,
  merchantName: "Wendy's — Rt 17",
  merchantAddress: null,
  dropoffAddress: "Morristown",
  statedDistanceMi: 4.2,
  statedMinutes: 23,
  deliverByClockTime: null,
  offerType: "single" as const,
  platform: "doordash" as const,
  peakPay: null,
  itemCount: null,
  totalMayBeHigher: false,
  isOfferCard: true,
  extractionConfidence: 0.95,
  notes: null,
};

describe("ExtractedOfferSchema", () => {
  it("accepts a well-formed extraction", () => {
    expect(ExtractedOfferSchema.safeParse(valid).success).toBe(true);
  });

  it("allows every card field to be absent", () => {
    // The model must be able to say "the card does not show this" rather than
    // inventing a plausible number.
    const blank = {
      ...valid,
      displayedPayout: null, merchantName: null, dropoffAddress: null,
      statedDistanceMi: null, statedMinutes: null,
      isOfferCard: false, extractionConfidence: 0,
    };
    expect(ExtractedOfferSchema.safeParse(blank).success).toBe(true);
  });

  it("rejects a confidence outside 0..1", () => {
    expect(ExtractedOfferSchema.safeParse(
      { ...valid, extractionConfidence: 1.4 }).success).toBe(false);
    expect(ExtractedOfferSchema.safeParse(
      { ...valid, extractionConfidence: -0.1 }).success).toBe(false);
  });

  it("rejects a payout that is not a number", () => {
    expect(ExtractedOfferSchema.safeParse(
      { ...valid, displayedPayout: "9.75" }).success).toBe(false);
  });

  it("rejects an unknown offer type", () => {
    expect(ExtractedOfferSchema.safeParse(
      { ...valid, offerType: "catering" }).success).toBe(false);
  });

  it("requires the isOfferCard discriminator", () => {
    const { isOfferCard: _omitted, ...without } = valid;
    expect(ExtractedOfferSchema.safeParse(without).success).toBe(false);
  });

  it("describes every field for the model", () => {
    // The descriptions are the prompt as far as the model is concerned; an
    // undescribed field gets guessed at.
    for (const [name, field] of Object.entries(ExtractedOfferSchema.shape)) {
      expect(field.description, `${name} has no description`).toBeTruthy();
    }
  });
});

describe("ExtractResponseSchema", () => {
  it("accepts a success envelope", () => {
    expect(ExtractResponseSchema.safeParse(
      { ok: true, offer: valid, error: null, latencyMs: 1200 }).success).toBe(true);
  });

  it("accepts a failure envelope with no offer", () => {
    expect(ExtractResponseSchema.safeParse(
      { ok: false, offer: null, error: "unreadable", latencyMs: 40 }).success).toBe(true);
  });

  it("rejects a response missing latency", () => {
    expect(ExtractResponseSchema.safeParse(
      { ok: true, offer: valid, error: null }).success).toBe(false);
  });
});
