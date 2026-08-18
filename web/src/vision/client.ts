/**
 * Browser side of vision extraction: screenshot in, structured offer out.
 *
 * The engine is local and works offline, but *reading the card* needs the
 * network. When that is unavailable this returns a clear failure rather than a
 * guess — an invented payout produces a confident wrong verdict, which is the
 * one outcome worse for the driver than no verdict.
 */

import type { ExtractedOffer, ExtractResponse } from "./schema";
import { ExtractResponseSchema } from "./schema";
import type { Offer, OfferType, Platform } from "../domain/models";
import { makeOffer } from "../domain/models";

/** Beyond this the offer timer has usually expired; failing fast beats hanging. */
const EXTRACT_TIMEOUT_MS = 20_000;

export interface ExtractionResult {
  offer: Offer | null;
  extracted: ExtractedOffer | null;
  confidence: number;
  error: string | null;
  latencyMs: number;
}

const OFFLINE: ExtractionResult = {
  offer: null, extracted: null, confidence: 0,
  error: "You're offline — can't read the card. This one's your call.",
  latencyMs: 0,
};

export async function fileToBase64(
  file: Blob,
): Promise<{ base64: string; mediaType: string }> {
  const buf = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  // Chunked to avoid blowing the argument limit on large screenshots.
  for (let i = 0; i < buf.length; i += 8192) {
    binary += String.fromCharCode(...buf.subarray(i, i + 8192));
  }
  return {
    base64: btoa(binary),
    mediaType: file.type || "image/png",
  };
}

/** Parse a deliver-by clock time into minutes from now, for cards that show
 *  a deadline instead of a duration. */
export function minutesUntilClockTime(clock: string, now: Date): number | null {
  const m = /^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$/.exec(clock.trim());
  if (!m) return null;
  const [, hh, mm, ampm] = m;
  let hour = Number(hh);
  const minute = Number(mm);
  if (!Number.isFinite(hour) || !Number.isFinite(minute) || minute > 59) return null;
  if (ampm) {
    const pm = ampm.toLowerCase() === "pm";
    if (hour === 12) hour = pm ? 12 : 0;
    else if (pm) hour += 12;
  }
  const target = new Date(now);
  target.setHours(hour, minute, 0, 0);
  // A deadline that already passed today refers to tomorrow.
  if (target.getTime() < now.getTime()) target.setDate(target.getDate() + 1);
  const minutes = (target.getTime() - now.getTime()) / 60000;
  return minutes > 0 && minutes < 24 * 60 ? minutes : null;
}

const toPlatform = (p: ExtractedOffer["platform"]): Platform =>
  p === "unknown" ? "doordash" : p;

/**
 * Convert a vision extraction into an engine Offer.
 *
 * Returns null when the payout is missing: there is nothing to decide without
 * it, and the engine must not be handed a zero that looks like a real number.
 */
export function toOffer(
  extracted: ExtractedOffer,
  offerId: string,
  now: Date,
  knownCapValue: number | null = null,
): Offer | null {
  if (!extracted.isOfferCard || extracted.displayedPayout === null) return null;

  const stated = extracted.statedMinutes ??
    (extracted.deliverByClockTime
      ? minutesUntilClockTime(extracted.deliverByClockTime, now)
      : null);

  const payout = extracted.displayedPayout;
  const atCap = extracted.totalMayBeHigher ||
    (knownCapValue !== null && Math.abs(payout - knownCapValue) < 0.005);

  return makeOffer({
    offerId,
    platform: toPlatform(extracted.platform),
    seenAt: now,
    displayedPayout: payout,
    merchantName: extracted.merchantName ?? "",
    merchantId: extracted.merchantName,
    merchantAddress: extracted.merchantAddress ?? "",
    dropoffAddress: extracted.dropoffAddress ?? "",
    statedDistanceMi: extracted.statedDistanceMi,
    statedMinutes: stated,
    offerType: extracted.offerType as OfferType,
    peakPay: extracted.peakPay ?? 0,
    hitDisplayCap: atCap,
    itemCount: extracted.itemCount,
  });
}

export async function extractOffer(
  image: Blob,
  offerId: string,
  now: Date,
  knownCapValue: number | null = null,
): Promise<ExtractionResult> {
  if (typeof navigator !== "undefined" && navigator.onLine === false) return OFFLINE;

  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), EXTRACT_TIMEOUT_MS);
  try {
    const { base64, mediaType } = await fileToBase64(image);
    const res = await fetch("/api/extract", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ imageBase64: base64, mediaType }),
      signal: controller.signal,
    });
    const parsed = ExtractResponseSchema.safeParse(await res.json());
    if (!parsed.success) {
      return { offer: null, extracted: null, confidence: 0,
               error: "Unexpected response from the reader.",
               latencyMs: Date.now() - started };
    }
    const body: ExtractResponse = parsed.data;
    if (!body.ok || !body.offer) {
      return { offer: null, extracted: null, confidence: 0,
               error: body.error ?? "Couldn't read that one.",
               latencyMs: body.latencyMs };
    }
    const extracted = body.offer;
    return {
      offer: toOffer(extracted, offerId, now, knownCapValue),
      extracted,
      confidence: extracted.isOfferCard ? extracted.extractionConfidence : 0,
      error: extracted.isOfferCard ? null : "That doesn't look like an offer card.",
      latencyMs: body.latencyMs,
    };
  } catch (err) {
    const aborted = err instanceof DOMException && err.name === "AbortError";
    return {
      offer: null, extracted: null, confidence: 0,
      error: aborted ? "Reader timed out — your call." : "Couldn't reach the reader.",
      latencyMs: Date.now() - started,
    };
  } finally {
    clearTimeout(timer);
  }
}
