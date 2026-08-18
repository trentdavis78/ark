# BLACKTOP — Architecture

This document maps the repository to the system described in `PRD.md` §7. It covers the
module layout, data flow, and the relationship between the **Python reference
implementation** (`core/`) and the **Android client** (`android/`).

## High-level shape

BLACKTOP is an offline-first, on-device decision engine. All inference runs locally on the
driver's phone; the backend exists only for durable sync, model retraining, and model
artifact distribution. The verdict path never touches the network.

```
offer appears on screen
        │
        ▼
  capture (AccessibilityService node read | MediaProjection+OCR)   ── android/capture
        │
        ▼
  parse (field extraction + confidence score)                      ── core/parser.py, android/parse
        │  low confidence ──► manual-entry fallback (I6)           ── android/ui/ManualEntryScreen
        ▼
  enrich (merchant wait, building friction, zone deadhead)         ── core/{merchant_oracle,building_intel,zone_map}.py
        │
        ▼
  value + time model → E[$/hr]                                     ── core/verdict.py, core/tip_estimator.py
        │
        ▼
  compare vs. dynamic reservation rate w*                          ── core/reservation.py
        │
        ▼
  Verdict {GREEN|AMBER|RED, $/hr, threshold, TTS line}             ── android/overlay (HUD + TTS)
```

## Repository map

| Path | Role | Verified here? |
|---|---|---|
| `core/` | Python 3.11 reference implementation of every algorithm. Stdlib-only. The semantic source of truth; the Kotlin engine is a port of these files. | Yes — full pytest suite |
| `core/blacktop/models.py` | Shared dataclasses/enums used by every module (Offer, Verdict, Session, Merchant, Building, ZoneStats, VehicleCostParams, TaxParams, …). | Yes |
| `core/blacktop/verdict.py` | F1 Offer Verdict Engine: time model (5-term T_total incl. `T_return_to_density`), value model, verdict emission with parse-confidence gating. | Yes |
| `core/blacktop/tip_estimator.py` | F2 Hidden-Tip Estimator: hierarchical partial pooling (market → category → merchant), display-cap detector, `hit_display_cap` boost. | Yes |
| `core/blacktop/zone_map.py` | F3 Zone Alpha Map: pluggable hex index (pure-python grid; H3 drop-in later), quality-density stats, positioning recommendation, seam detection, deadhead surface. | Yes |
| `core/blacktop/merchant_oracle.py` | F4 Merchant Wait Oracle: streaming P50/P90 per location × hour-of-week with sparse-data fallback, chronic-offender flag, bounded arrival-timing advice. | Yes |
| `core/blacktop/building_intel.py` | F5 Last-100-Feet Intel: knowledge base keyed by building, friction estimation, preset-tap and free-text note capture, share-gating (I5/F14 rules). | Yes |
| `core/blacktop/reservation.py` | F6 Dynamic Reservation Rate: renewal-reward optimal-stopping fixed point, session-end decay, AR-farming policy layer, guardrails, sanity band clamp. | Yes |
| `core/blacktop/mileage_tax.py` | F7 Mileage & Tax: 2026 split IRS rate (72.5¢ ≤ Jun 30, 76¢ ≥ Jul 1), audit log, true net, shielded income counter, SE tax, 1040-ES + NJ-1040-ES, vehicle cost model. | Yes |
| `core/blacktop/parser.py` | Offer-card text → structured `ParsedOffer` with per-field confidence; manual-entry constructor (I6). | Yes |
| `core/blacktop/counterfactual.py` | F13 threshold counterfactual replay over the logged offer stream, threshold sweep report. | Yes |
| `android/` | Kotlin/Jetpack Compose client. Source-complete; **not compiled in this environment** (no Android SDK/Gradle here). | Written, unverified |
| `backend/supabase/` | Postgres+PostGIS migration for PRD §8 tables with RLS keyed to `driver_id`; edge function stubs (sync ingest, model artifact signed URL). | Written, unverified |
| `ml/` | LightGBM training + ONNX export pipeline for F2; feature engineering shared with core. Tests skip gracefully when lightgbm/onnx are absent. | Yes (guarded) |

## Android module map (`android/app/src/main/java/com/blacktop/app/`)

- `capture/` — `OfferCaptureAccessibilityService` (**read-only** node traversal; no
  `performAction` ever — I3), `ScreenCaptureManager` (MediaProjection scaffold).
- `parse/` — ML Kit OCR wrapper + `OfferCardParser`, a faithful port of `core/blacktop/parser.py`.
- `engine/` — Kotlin ports of `verdict.py`, `reservation.py`, and tip-estimator inference
  (ONNX Runtime Mobile at runtime; pooled-prior fallback before the model ships).
- `data/` — Room entities/DAOs mirroring PRD §8 tables 1:1.
- `overlay/` — HUD bubble, verdict rendering (one colored number vs. threshold), TTS.
- `tracking/` — foreground-service GPS session logger, geofence dwell tracker (feeds F4/F7).
- `ui/` — Compose screens: dashboard, **manual entry (first-class, I6)**, session, settings,
  informed-consent screen gating screen-reading enable.

## Data flow and caching

- **Routing cache is load-bearing** (PRD §7): the reference engine takes travel times as
  injected inputs (`RouteProvider` protocol) so the Android client can back it with a
  cached matrix + live routing, and tests can drive it deterministically.
- **Every offer is an observation**: declines are logged and feed `zone_map` and
  `counterfactual` exactly like accepts.
- **Sync** is batched and offline-tolerant; the Supabase edge function ingests rows and
  RLS scopes everything to `driver_id`.

## Deliberate reference-implementation simplifications

- The hex index is a pure-python equal-area-ish grid (`GridHexIndex`) behind a `HexIndex`
  protocol; production swaps in H3 res-8/9 without touching zone logic.
- The tip model in `core/` is the partial-pooling estimator (the cold-start path in the
  PRD); the LightGBM/ONNX pipeline in `ml/` is the trained-model path and shares the F2
  feature list via `ml/features.py`.
- Routing, weather, and geocoding are injected interfaces, not live calls (offline-first,
  and no network in tests).
