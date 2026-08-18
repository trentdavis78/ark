/**
 * The contract between the vision model and the decision engine.
 *
 * Shared by the server handler (which constrains Claude's output to it) and
 * the browser client (which validates what comes back). One definition, so a
 * field cannot drift between the two.
 */

import { z } from "zod";

/**
 * Every field is nullable on purpose. The model must be able to say "the card
 * does not show this" rather than inventing a plausible number — a fabricated
 * distance produces a confident wrong verdict, which is worse for the driver
 * than no verdict at all.
 */
export const ExtractedOfferSchema = z.object({
  displayedPayout: z.number().nullable()
    .describe("The guaranteed/displayed dollar total, e.g. 9.75. Null if not visible."),
  merchantName: z.string().nullable()
    .describe("Restaurant or store name exactly as printed, e.g. \"Wendy's\"."),
  merchantAddress: z.string().nullable()
    .describe("Pickup address or town if the card shows one."),
  dropoffAddress: z.string().nullable()
    .describe("Dropoff address or area. Often partial, e.g. \"Morristown\"."),
  statedDistanceMi: z.number().nullable()
    .describe("Distance in miles exactly as the card states it, e.g. 4.2."),
  statedMinutes: z.number().nullable()
    .describe("Total minutes the card states. Null if it only gives a deliver-by clock time."),
  deliverByClockTime: z.string().nullable()
    .describe("A deliver-by time if shown instead of a duration, e.g. \"7:42 PM\"."),
  offerType: z.enum(["single", "stacked", "shop_deliver", "large_order"])
    .describe("Stacked when the card shows two orders; shop_deliver for shop-and-deliver."),
  platform: z.enum(["doordash", "uber_eats", "grubhub", "unknown"])
    .describe("Which app's offer card this is, judged from its layout and branding."),
  peakPay: z.number().nullable()
    .describe("Peak Pay / promotion bonus shown as a separate line item, if any."),
  itemCount: z.number().nullable().describe("Item count if the card shows one."),
  totalMayBeHigher: z.boolean()
    .describe("True if the card carries a 'total may be higher' style disclaimer."),
  isOfferCard: z.boolean()
    .describe("False if this screenshot is not a delivery offer card at all."),
  extractionConfidence: z.number().min(0).max(1)
    .describe(
      "Your confidence that the fields above are correct, 0..1. Be strict: " +
      "score below 0.75 whenever the image is blurred, cropped, partly " +
      "occluded, or you had to guess any value.",
    ),
  notes: z.string().nullable()
    .describe("Anything unusual worth surfacing, e.g. a red 'low tip' warning."),
});

export type ExtractedOffer = z.infer<typeof ExtractedOfferSchema>;

/** What the endpoint returns to the browser. */
export const ExtractResponseSchema = z.object({
  ok: z.boolean(),
  offer: ExtractedOfferSchema.nullable(),
  error: z.string().nullable(),
  latencyMs: z.number(),
});

export type ExtractResponse = z.infer<typeof ExtractResponseSchema>;
