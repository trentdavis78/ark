/**
 * Vision extraction endpoint — reads an offer-card screenshot with Claude.
 *
 * This runs server-side for one non-negotiable reason: the Anthropic API key
 * must never reach the browser. The handler takes a fetch-standard Request and
 * returns a Response, so the same code runs under Vite in dev and on Supabase
 * Edge Functions, Vercel, or Cloudflare in production.
 *
 * Compliance: the image is a screenshot the driver took of their own screen
 * and chose to share. Nothing here touches a platform server or a platform
 * credential (I1/I2), and the result is advisory only (I4).
 */

import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { ExtractedOfferSchema } from "../vision/schema";

/** Screenshots above this are rejected rather than silently downscaled. */
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

const ALLOWED_MEDIA_TYPES = [
  "image/png", "image/jpeg", "image/webp", "image/gif",
] as const;

type AllowedMediaType = (typeof ALLOWED_MEDIA_TYPES)[number];

const SYSTEM_PROMPT = `You read delivery-driver offer cards from screenshots and return their fields.

The person reading your output is driving. They have about 30 seconds to accept or decline, and a wrong number costs them real money — so accuracy matters far more than completeness.

Rules:
- Transcribe only what is printed. Never infer, average, or estimate a value that is not on screen.
- A field the card does not show is null. Do not substitute a typical value.
- The payout is the large guaranteed total, not a Peak Pay line item or a subtotal.
- Distance on these cards is straight-line, not driving distance. Report it exactly as printed anyway; the engine corrects for it.
- Set totalMayBeHigher when the card hedges the amount ("total may be higher", a trailing "+"). This signals the tip is capped and is a strong downstream predictor.
- Score extractionConfidence strictly. Below 0.75 the app shows no verdict and hands the decision back to the driver, which is the correct outcome for an unreadable image.
- If the screenshot is not an offer card, set isOfferCard false and everything else null.`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function isAllowedMediaType(v: string): v is AllowedMediaType {
  return (ALLOWED_MEDIA_TYPES as readonly string[]).includes(v);
}

export async function extractHandler(request: Request): Promise<Response> {
  const started = Date.now();
  if (request.method !== "POST") {
    return json({ ok: false, offer: null, error: "POST required", latencyMs: 0 }, 405);
  }

  let payload: { imageBase64?: unknown; mediaType?: unknown };
  try {
    payload = (await request.json()) as typeof payload;
  } catch {
    return json({ ok: false, offer: null, error: "malformed JSON body", latencyMs: 0 }, 400);
  }

  const { imageBase64, mediaType } = payload;
  if (typeof imageBase64 !== "string" || imageBase64.length === 0) {
    return json({ ok: false, offer: null, error: "imageBase64 required", latencyMs: 0 }, 400);
  }
  if (typeof mediaType !== "string" || !isAllowedMediaType(mediaType)) {
    return json({
      ok: false, offer: null, latencyMs: 0,
      error: `mediaType must be one of ${ALLOWED_MEDIA_TYPES.join(", ")}`,
    }, 400);
  }
  // base64 encodes 3 bytes per 4 characters.
  if ((imageBase64.length * 3) / 4 > MAX_IMAGE_BYTES) {
    return json({
      ok: false, offer: null, latencyMs: 0,
      error: `screenshot exceeds ${MAX_IMAGE_BYTES / (1024 * 1024)}MB`,
    }, 413);
  }

  const client = new Anthropic();
  try {
    const response = await client.messages.parse({
      model: "claude-opus-5",
      max_tokens: 2048,
      system: SYSTEM_PROMPT,
      messages: [
        {
          role: "user",
          content: [
            { type: "image", source: { type: "base64", media_type: mediaType, data: imageBase64 } },
            { type: "text", text: "Extract this offer card." },
          ],
        },
      ],
      output_config: {
        // Transcription, not reasoning — low effort keeps latency inside the
        // offer timer. Thinking stays adaptive (the default): disabling it on
        // Opus 5 risks tool calls and tags leaking into visible text.
        effort: "low",
        format: zodOutputFormat(ExtractedOfferSchema),
      },
    });

    // A safety refusal returns HTTP 200 with no usable content, so check the
    // stop reason before reading the parsed output.
    if (response.stop_reason === "refusal") {
      return json({
        ok: false, offer: null, latencyMs: Date.now() - started,
        error: "the model declined to process this image",
      }, 422);
    }

    const offer = response.parsed_output;
    if (!offer) {
      return json({
        ok: false, offer: null, latencyMs: Date.now() - started,
        error: "extraction produced no structured output",
      }, 502);
    }

    return json({ ok: true, offer, error: null, latencyMs: Date.now() - started });
  } catch (err) {
    // Never leak the key or internals to the client; the driver needs one
    // clear fact — this one has to be their call.
    const message = err instanceof Anthropic.APIError
      ? `vision service error (${err.status ?? "network"})`
      : "vision service unavailable";
    return json({ ok: false, offer: null, error: message, latencyMs: Date.now() - started }, 502);
  }
}

/** Default export for fetch-standard runtimes (Vercel, Cloudflare, Deno). */
export default { fetch: extractHandler };
