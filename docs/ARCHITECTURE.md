# Architecture

Maps this repository to the system in [`PRD.md`](PRD.md) §7, and records where
it deliberately departs from it.

## The departure

The PRD specifies an Android client because the core feature — reading the
offer card off the screen and floating a verdict over it — is impossible on
iOS. This repository ships a PWA instead, which cannot do either of those
things on any platform.

What replaces silent capture is a screenshot the driver shares. What replaces
the overlay is the app itself. The cost is PRD §4's sub-400ms budget: a vision
round trip is seconds, not milliseconds. It still lands inside a 30–45 second
offer timer, but it is a different interaction — three taps, not zero.

What it buys is the compliance position. No accessibility service, no screen
reading, no overlay, and therefore none of the gray zone PRD §5.1 identifies as
the product's largest risk. See [`COMPLIANCE.md`](COMPLIANCE.md).

## Flow

```
  driver screenshots the offer card
        │
        │  Android share sheet ──► service worker POST catch ──► /?shared=1
        │  or drag-drop / paste / file picker
        ▼
  POST /api/extract  { imageBase64, mediaType }        web/src/server/extract.ts
        │                                              (server-side: the API key
        │                                               must never reach a browser)
        ▼
  claude-opus-5, output constrained to a zod schema    web/src/vision/schema.ts
        │
        │  confidence < 0.75, or no payout, or not an offer card
        │       └──► no verdict. The driver decides, and is told why.
        ▼
  ExtractedOffer ──► Offer                             web/src/vision/client.ts
        │
        ▼
  five-term time model                                 web/src/domain/verdict.ts
    drive to merchant + merchant wait (F4)
      + drive to customer + dropoff friction
      + return to density  ← the deadhead nobody models
        │
        ▼
  E[payout] = displayed + E[hidden tip | features]     web/src/domain/tipEstimator.ts
        │
        ▼
  compare vs. w*(now, context)                         web/src/domain/reservation.ts
        │
        ▼
  Verdict {green|amber|red|manual_fallback}            web/src/app/main.ts
    one colour, one number, one spoken line
```

Everything after extraction is local and works offline. Extraction needs the
network; when it is unavailable the app says so rather than guessing.

## Repository map

| Path | Role | Verified here |
|---|---|---|
| `web/src/domain/` | The engine. No DOM, no network, no storage — pure functions over value objects. | 54 tests |
| `web/src/vision/` | Schema shared by server and client; screenshot → `Offer` conversion. | 19 tests |
| `web/src/server/extract.ts` | The only place an API key exists. Fetch-standard handler. | typecheck; bundle asserted key-free |
| `web/src/app/` | UI, IndexedDB persistence, session state wiring the learned models together. | typecheck, production build |
| `web/public/sw.js` | Offline shell + the share-target POST catcher. | — |
| `core/blacktop/` | Python reference implementation of every algorithm, stdlib only. Semantic source of truth. | 219 tests |
| `core/blacktop/sim/` | The shift simulator: generative NJ market, matched-baseline runner, clairvoyant ceiling. | included above |
| `db/schema.sql` | Postgres + PostGIS + RLS, per PRD §8. Not yet wired to the app. | parsed by libpg_query |

## Why two implementations

`core/` is Python and `web/src/domain` is TypeScript, and they implement the
same algorithms. That duplication is deliberate and bounded.

The simulator has to replay tens of thousands of shifts to say anything useful
about a policy, and it needs to be trivially scriptable. That is not browser
work. So `core/` is where a change to the decision math gets *judged* — the
zone-level lift assertions in `core/tests/test_sim.py` are what catch a
regression that unit tests miss — and `web/src/domain` is the port that ships.

The risk is drift. The mitigation is that both sides carry the same tests for
the same properties, in the same order, with the same names, so a change on one
side that is not mirrored shows up as a missing test rather than a silent
divergence.

## The reservation rate

`web/src/domain/reservation.ts` and `core/blacktop/reservation.py` are the
subtlest part of the system, and four of their properties are load-bearing.
Each was established by measuring a policy that lacked it; each failure mode
was the same — the engine silently degenerating into the fixed threshold it
exists to replace. [`SIMULATION.md`](SIMULATION.md) has the measurements.

1. **The sanity band diagnoses, never clamps.** Clamping to the band floor
   declines every offer a lean market can produce.
2. **The window survives starvation.** A working driver sees few offers, so the
   window reaches back for a minimum sample count and pools toward a learned
   prior rather than snapping to a constant.
3. **Windows are keyed by context.** One market must not set another's
   threshold.
4. **λ counts minutes available, not elapsed.** Offers arriving mid-delivery
   are never seen; ignoring that depresses the threshold and over-accepts.

## Not built

`db/schema.sql` exists but nothing syncs to it — the app is local-only, storing
offers and sessions in IndexedDB. F3 (zone map), F5 (building intel), and F13
(counterfactual replay) exist in `core/` and are exercised by the simulator but
are not wired into the app. F8–F11 are not implemented at all.
