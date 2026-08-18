# PRD — Project BLACKTOP

### A real-time decision engine for gig delivery drivers, tuned for North/Central New Jersey

| | |
|---|---|
| **Version** | 0.1 (Draft) |
| **Date** | August 18, 2026 |
| **Owner** | TD |
| **Status** | Pre-build |
| **Target platform** | Android first (hard constraint — see §5.3) |

---

## 1. Problem

A delivery driver makes 40–120 accept/decline decisions per shift, each under a 30–45 second timer, while driving. Each decision is made against an offer card that deliberately withholds the single most important variable (the tip) and misrepresents two others (travel time as straight-line distance, and pickup wait as zero).

The driver's response to this is a static heuristic — usually "$2 per mile" — applied uniformly across contexts where the correct threshold varies by a factor of three. The result is systematic, compounding value destruction: accepting orders worth $15/hr in dense zones, declining orders worth $50/hr in sparse ones, and being blind to costs the platform never surfaces (deadhead repositioning, merchant wait, parking risk, tower access friction).

Existing tools do not solve this. Mileage trackers (Everlance, Stride) are accounting, not decisions. Earnings aggregators (Gridwise, Solo) are reporting, not decisions. The one product that attacked the information asymmetry directly — Para — was disabled in 2021 when DoorDash stripped the full payout from the offer payload sent to the driver's device, and has been degraded ever since.

**Nobody is building the decision layer.**

## 2. Thesis

Three claims, in order of confidence:

1. **The objective function is wrong.** Drivers optimize gross dollars per mile. The correct objective is net dollars per online hour after true vehicle cost and after tax. Fixing only this is worth an estimated 8–15%.

2. **The policy class is wrong.** A fixed acceptance threshold is provably suboptimal for a problem that is structurally an optimal-stopping problem with a time-varying arrival process. A dynamic reservation rate computed from real-time opportunity cost is the correct policy. Worth an estimated 10–20% on top.

3. **The private data compounds.** Merchant wait distributions, building access procedures, parking risk, and per-destination friction are learnable from the driver's own observations, are unavailable to competing drivers, and improve monotonically with usage. This is the moat and it is entirely ToS-clean.

BLACKTOP is the product that operationalizes all three.

## 3. Goals & non-goals

### Goals

- **G1** — Deliver an accept/decline verdict in under 400ms P95 from offer appearance, glanceable in under one second, safe to consume while driving.
- **G2** — Improve the driver's measured net hourly by ≥20% over a matched 30-day baseline.
- **G3** — Capture ≥95% of legally deductible business miles automatically, with an audit-defensible log.
- **G4** — Build a per-driver operational knowledge base (merchants, buildings, parking) that materially reduces time-per-delivery over time.
- **G5** — Never put the driver's account at risk. Zero deactivations attributable to BLACKTOP.

### Non-goals

- **NG1** — No automated accepting or declining. Ever. The human makes every tap. This is not a caution, it's an architectural invariant (§5.1).
- **NG2** — No credential collection. BLACKTOP never asks for and never stores DoorDash, Uber, or any platform login.
- **NG3** — No calls to platform APIs, authenticated or otherwise. No scraping of platform servers.
- **NG4** — Not a general-purpose bookkeeping app. Mileage and tax exist because they're the largest financial lever, not because we're competing with QuickBooks.
- **NG5** — v1 is not multi-tenant SaaS. Single operator, then a small closed beta.

## 4. Success metrics

**North star:** net dollars per online hour, after modeled true vehicle cost and after estimated tax.

| Metric | Target (v1) |
|---|---|
| Net $/online-hour lift vs. 30-day baseline | +20% |
| Verdict latency, P95 | < 400ms |
| Offer card parse success rate | > 97% |
| Hidden-tip prediction MAE | < $2.10 after 300 labeled deliveries |
| Merchant wait prediction MAE | < 3.5 min after 20 visits/location |
| Deductible miles captured vs. manual audit | > 95% |
| Completion rate (guardrail) | ≥ 95% sustained |
| Customer rating (guardrail) | ≥ 4.7 sustained |
| Contract violations (guardrail) | 0 |
| Account deactivations (guardrail) | 0 |

Guardrails are release-blocking. A feature that lifts hourly but degrades completion rate ships disabled.

---

## 5. Constraints

### 5.1 The compliance envelope — read before designing anything

This section defines what BLACKTOP is allowed to do, and it is not negotiable. Para's tip transparency feature worked by taking the driver's DoorDash credentials, intercepting the JSON payload sent to the device, and parsing the full payout out of it. DoorDash killed it by removing the payout from the payload, and the product never recovered.

The lesson is not "don't do useful things." It's that any capability which depends on a platform's cooperation, credentials, or server responses is a capability the platform can revoke unilaterally and without notice. **Build only on things the platform cannot take away.**

The invariants:

| # | Invariant | Rationale |
|---|---|---|
| **I1** | No platform credentials are requested, transmitted, or stored. | Credential sharing is the clearest ToS violation and the clearest deactivation trigger. |
| **I2** | No network requests to platform servers. All platform data comes from what is already rendered on the driver's own screen. | Removes the entire API-access risk surface. |
| **I3** | No synthetic input events. BLACKTOP never taps, never accepts, never declines. | Automation of platform interaction is the fastest route to deactivation, and it's the line between "tool" and "bot." |
| **I4** | Advisory only. Every output is a recommendation displayed to a human who then acts. | Preserves the driver as the decision-maker in fact, not just in form. |
| **I5** | All learned data is the driver's own observation of their own work. | Your delivery history is your business record. |
| **I6** | Manual-entry fallback mode ships in v1 and works standalone. | If screen reading breaks or is disabled, the product still functions. |

**The residual risk, stated plainly:** on-device reading of the offer card via OCR or accessibility services is a gray zone. It touches no platform server, uses no credentials, and reads only data the platform already chose to render to this driver on this device. That's materially different from what Para did. It is still not explicitly blessed by any platform's terms, and a driver should understand that before enabling it. This is why I6 exists — the fallback mode is not a nice-to-have, it's the risk hedge for the entire product.

### 5.2 Physical constraints

- The driver is operating a vehicle. Any output requiring more than one second of visual attention is a safety failure and a design failure.
- Offer timers run 30–45 seconds. A verdict arriving at second 20 is worthless.
- Phone is mounted, often in direct sun, often at low battery, often on degraded cellular.

### 5.3 Platform constraint

Android only for v1. iOS does not permit an app to observe another app's rendered content or draw persistent overlays over it. This is not a prioritization decision — the core feature is technically impossible on iOS. Distribution may require sideloading or a direct APK channel if Play Store accessibility-service policy proves restrictive; budget for this.

---

## 6. Feature specification

### P0 — Core loop

---

#### F1. Offer Verdict Engine

*The product. Everything else supports this.*

On offer appearance, extract the offer card fields, compute a projected net hourly value, compare against the current dynamic reservation rate, and emit a verdict.

**Extraction:** on-device OCR (ML Kit) over a captured region, or AccessibilityService node traversal. Fields: displayed payout, merchant name, merchant address, dropoff address (often partial), stated distance, stated time, offer type (single / stacked / Shop & Deliver / large order), any Peak Pay indicator.

**Time model:**

```
T_total = T_drive_to_merchant       (routing engine, live traffic)
        + T_merchant_wait           (F4, learned per location × hour-of-week)
        + T_drive_to_customer       (routing engine, live traffic)
        + T_dropoff_friction        (F5, learned per building/destination class)
        + T_return_to_density       (F3, deadhead to next likely offer)
```

`T_return_to_density` is the differentiator. No platform surfaces it and no competing product models it. It is computed as the expected travel time from the dropoff point to the nearest hex with sufficient offer arrival rate for the current hour. On a suburban long-haul it can exceed the entire delivery time.

**Value model:**

```
E[payout] = displayed_payout + E[hidden_tip | features]     (F2)
E[$/hr]   = E[payout] / T_total × 60
verdict   = E[$/hr] vs. reservation_rate(now, zone, weather, arrival_rate)   (F6)
```

**Output:** a single colored number plus an audio cue. Green / amber / red. Optional TTS: *"Take it. Thirty-eight."* Nothing to read, nothing to tap.

**Latency budget:**

| Stage | Budget |
|---|---|
| Capture + OCR | 120ms |
| Geocode + route (cached where possible) | 150ms |
| Model inference (local) | 40ms |
| Render + audio | 50ms |
| **Total P95** | **< 400ms** |

Routing calls must be aggressively cached — merchant locations repeat constantly, and a per-driver origin/destination matrix cache will hit most of the time.

---

#### F2. Hidden-Tip Estimator

Predicts the concealed portion of the payout. The label arrives for free: after delivery completion the driver sees the actual payout, which is compared to what the offer card displayed. Every completed delivery is a training example.

**Features:** merchant category, merchant identity, item count if visible, order subtotal if visible, dropoff geohash (H3 res 8), dropoff destination class, hour-of-week, weather, Peak Pay active, offer type, and — critically — a `hit_display_cap` boolean.

**The cap signal.** DoorDash appears to cap the displayed amount at a market-specific value with a "total may be higher" disclaimer. If offers repeatedly land on the identical dollar figure, that's the ceiling. Detect the modal cap value per market automatically and treat "at cap" as a strong positive predictor. This is the highest-signal feature in the model and it requires no privileged access — just counting.

**The Uber Eats cross-calibration play.** Uber Eats displays guaranteed totals inclusive of tip. Same restaurants, same neighborhoods, same customer population. Ingesting UE offer cards alongside DD offer cards gives a *fully observed* tip distribution for the same underlying geography, which serves as a prior for the DD model. Nobody else is doing this. Ship in P1.

**Model:** gradient-boosted trees (LightGBM), exported to ONNX for on-device inference. GBTs beat neural nets decisively at this data scale and feature type. Cold start via hierarchical partial pooling — market-level prior, driver-level posterior — so the model is useful at delivery 20, not delivery 500.

---

#### F3. Zone Alpha Map

A learned surface of expected net $/hr by H3 hex × hour-of-week × weather bucket.

The key distinction from DoorDash's own hotspot map: hotspots show *offer density*. BLACKTOP shows **offer quality density**. Paramus at 6pm has enormous offer volume and mediocre offer quality. A hex in downtown Morristown may show a third the volume and twice the expected hourly.

Also produces:

- **Positioning recommendation** — "Move 2.4mi to hex X. Expected +$8.40/hr. Drive cost 7 min. Break-even at 21 min remaining in session."
- **Seam detection** — automatically surfaces boundary positions between a high-volume hex cluster and a high-tip hex cluster, which is the highest-value positioning play in this region (see playbook §1B).
- **The deadhead surface** that F1 consumes.

Bootstrapped from public POI density and merchant counts; refined continuously from the driver's own offer stream (including declined offers — a declined offer is still an observation about that hex).

---

#### F4. Merchant Wait Oracle

Per-*location* pickup wait distribution — P50 and P90 by hour-of-week. Not per-brand; the same chain behaves completely differently four miles apart.

Measured passively from geofence dwell time: arrival at merchant polygon → departure. No driver input required.

**Outputs:**

- Feeds `T_merchant_wait` into F1.
- Flags chronic offenders for pre-decline: *"This location runs 11 min at dinner, P90 18."*
- **Arrival timing advice:** if predicted wait is 12 min, tell the driver to arrive 6 min later. Food won't be ready regardless, and standing at a counter is pure unpaid loss. Bounded to never risk a lateness violation.

---

#### F5. Last-100-Feet Intel

*The moat.*

A knowledge graph keyed by building / complex / address, capturing everything that costs time at the destination.

**Schema per node:** access procedure, gate code, entry door, elevator bank, whether lobby handoff is accepted, loading dock availability, parking spot that doesn't get ticketed, unit numbering scheme, dog, stairs, walk time from nearest legal park.

**Capture UX must be nearly free or it won't happen.** On dropoff completion, a single voice prompt: *"Anything?"* Driver speaks; on-device transcription; LLM structured extraction into the schema. Two seconds of effort. Alternatively two taps from a preset list.

**Retrieval:** surfaced automatically when an offer's dropoff matches a known node — *before* the accept decision, so it feeds F1's friction estimate, and again on approach.

Optional federated layer (P2): anonymized, aggregated building intel shared across users. Vastly accelerates cold start and creates network effects. Gated behind explicit opt-in; no addresses of single-family residences ever shared, complexes and commercial buildings only.

**Why this is the moat:** it is unreplicable by competitors, unremovable by platforms, entirely composed of the driver's own observations, and it compounds. At 500 deliveries a driver has a private operational database of their territory that no other driver possesses.

---

#### F6. Dynamic Reservation Rate

The threshold that F1 compares against. Not a constant, not a user setting.

This is an optimal-stopping problem: the driver receives a stream of offers with values drawn from a time- and location-varying distribution, and must decide each in isolation. The optimal policy is a reservation rate `w*` satisfying the indifference condition — accept iff the offer's value rate exceeds the expected value of declining and continuing to search.

**Implementation:**

- Maintain a rolling estimate of the offer value distribution and arrival rate λ for the current hex cluster × hour × weather bucket, over a 20–40 minute window.
- `w*` is derived from the continuation value: roughly, the expected value rate obtainable by waiting for a better draw, discounted by expected wait time `1/λ`.
- Modulate by remaining session time — the reservation rate should decay toward the end of a planned session, because there's no future to wait for.
- Modulate by hard constraints — if acceptance rate is 3 points below the Priority Access threshold and the month ends in four days, temporarily lower `w*` to farm AR (see §Policy Constraints below).

**Sanity band:** `w*` should land roughly in the $0.30–$0.85 per projected minute range across contexts. If it leaves that band, something is wrong upstream.

**Policy constraints layer.** The reservation rate optimizer must respect:

- Completion rate ≥ 95% (never recommend an accept that risks an unassign)
- Acceptance rate targeting, zone-conditional — chase 50%+ in sparse zones where Priority Access changes offer flow, ignore it entirely in dense zones where it doesn't (playbook §2)
- Lateness violation avoidance

---

#### F7. Mileage & Tax Engine

Automatic session-boundary mileage logging: online → offline, capturing *all* miles including deadhead and repositioning, not just delivery legs. Most drivers log only delivery miles and forfeit 30–40% of their deduction.

**Requirements:**

- Correct application of the 2026 split rate: **72.5¢/mi** through June 30, **76¢/mi** from July 1. A tracker that applies a single annual rate to 2026 is wrong.
- Audit-defensible log: date, start/end time, start/end location, total miles, business purpose, route polyline.
- Live "true net" display: gross minus modeled true vehicle cost minus estimated tax reserve.
- **Shielded income counter** — a running display of how much of today's gross is tax-free due to the mileage deduction. This is the number that reframes the whole business for a driver, and no existing app surfaces it.
- Quarterly estimated payment calculator: federal 1040-ES + NJ-1040-ES, with SE tax at 15.3% on net.
- Vehicle cost model: user-supplied MPG or kWh/mi, fuel/electricity price, maintenance reserve per mile, depreciation. Produces the true cost/mile that the net calculation depends on.

---

### P1 — Multipliers

#### F8. Multi-App Arbitrage Layer

Simultaneous ingestion of DoorDash, Uber Eats, and Grubhub offer cards. Unified verdict against a single reservation rate. Critically: **collision detection** — flag any accept that would make an already-accepted order late. Stack feasibility scoring for multi-order runs. Also serves as the data source for F2's cross-calibration.

#### F9. Weather Alpha Engine

Ingest NWS / Open-Meteo HRRR. Storm days are the highest-earning days of the year because demand spikes while competing driver supply collapses 40–60%, and almost nobody schedules against forecasts.

Output: a 2–6 hour ahead advisory with historical analog comparison — *"Snow onset 4pm Thursday, Morris County. Analog days averaged $41/hr vs. $27 baseline. Schedule 3–9pm."* Push notification the evening before, when scheduling actually happens.

#### F10. Calendar Alpha

Ingest published schedules and pre-position: Rutgers academic calendar and home football, Princeton, Montclair State, Seton Hall, Kean, TCNJ, Stevens; MetLife, Prudential Center, Red Bull Arena, PNC Bank Arts Center, NJPAC, State Theatre, Stone Pony. Surfaces as scheduling recommendations days ahead, not as a same-day surprise.

#### F11. Ticket & Tow Shield

Geofenced parking risk layer for the aggressive-enforcement municipalities: Hoboken, Jersey City, Montclair, Morristown, Princeton, Red Bank, Asbury Park, Fort Lee, Summit, Westfield. Street cleaning schedules, meter hours, permit-only blocks, known enforcement corridors.

Alert on approach, not after: *"Resident permit block, enforcement active until 8pm. Nearest safe stop 40m north."*

A single avoided $60–100 ticket covers a year of subscription. This is the easiest feature to justify to a user in Hudson County.

#### F12. Voice-First HUD

Persistent Android overlay bubble showing live session net $/hr. Verdict delivered by tone and color, TTS optional over Bluetooth. Zero-touch operation as the default path. A driver should be able to run an entire shift without reading the screen once.

---

### P2 — Compounding

#### F13. Policy Self-Experimentation

Weekly counterfactual analysis over the driver's own logged offer stream:

> *"Your effective threshold was $0.62/min, producing $34.10/hr. Simulated at $0.55: $35.80/hr (+5.0%) — your zone's arrival rate supports higher volume. Simulated at $0.70: $31.20/hr (−8.5%)."*

Because every offer is logged including declines, the counterfactual is computable without experimentation. Where genuine exploration is needed, run a bounded Thompson-sampling bandit over threshold values with guardrails on completion rate.

**This is the feature that makes the product feel impossible.** An app that measurably improves its own recommendations against realized outcomes, and shows its work.

#### F14. Federated Intel Network

Opt-in anonymized sharing of merchant wait distributions, building access (commercial and multi-unit only), and parking risk. Massive cold-start acceleration and a genuine network effect. Strict exclusion of single-family residences, customer names, and order contents.

#### F15. Shift Planner

Given a target income and available hours, produce the optimal schedule for the coming week across zone, time block, weather forecast, and event calendar. The output of everything above, in one screen.

---

## 7. Architecture

```
┌─ ON DEVICE (Android / Kotlin) ────────────────────────────┐
│                                                            │
│  Capture      AccessibilityService | MediaProjection        │
│     ↓                                                       │
│  Parse        ML Kit OCR → field extraction → validation    │
│     ↓                                                       │
│  Enrich       local cache: merchants, buildings, hexes,     │
│               route matrix, zone stats                      │
│     ↓                                                       │
│  Infer        ONNX Runtime Mobile — tip model, wait model,  │
│               reservation rate policy         (< 40ms)      │
│     ↓                                                       │
│  Present      overlay + audio + TTS                         │
│                                                            │
│  Also: GPS session logger, geofence dwell tracker,          │
│        voice note capture, offline queue                    │
└────────────────────────────────────────────────────────────┘
                          ↕  (async, batched, offline-tolerant)
┌─ BACKEND ──────────────────────────────────────────────────┐
│                                                            │
│  Supabase — Postgres + PostGIS + RLS + Realtime             │
│  Edge Functions — sync, model distribution                  │
│  GCP Cloud Run + Scheduler — nightly retraining             │
│  Model artifacts → GCS → signed URLs → device               │
│                                                            │
│  External:                                                  │
│    Routing    Valhalla (self-hosted) or Google Routes API    │
│    Weather    NWS + Open-Meteo HRRR                          │
│    Geo        H3 (res 8 primary, res 9 for buildings)        │
└────────────────────────────────────────────────────────────┘
```

**Design principles:**

- **Offline-first.** Cellular in parts of Morris and Hunterdon counties is unreliable and the verdict cannot wait on a network round trip. All inference is local. Sync is opportunistic.
- **Routing cache is load-bearing.** Merchant→customer pairs repeat heavily within a zone. Cache aggressively with a traffic-adjusted staleness window. A cold routing call blows the latency budget.
- **Parse failures are telemetry, not silent errors.** When DoorDash changes the offer card layout — and they will — parse failure rate must spike a visible alert, and the app must degrade gracefully to manual-entry mode rather than emitting wrong verdicts.

**Stack summary**

| Layer | Choice | Why |
|---|---|---|
| Client | Kotlin, Jetpack Compose | Only platform where the core feature is possible |
| On-device ML | ONNX Runtime Mobile | GBT export, fast cold start, small binary |
| Training | Python, LightGBM, on Cloud Run | Right model class for tabular; existing GCP familiarity |
| Backend | Supabase (Postgres + PostGIS) | Existing familiarity, RLS handles multi-tenancy cleanly |
| Routing | Valhalla self-hosted, Google Routes fallback | Cost control at volume; matrix API for the deadhead surface |
| Geospatial | H3 | Fast, hierarchical, plays well with Postgres |
| Weather | NWS + Open-Meteo | Free, HRRR resolution is adequate for 6hr horizon |

---

## 8. Data model (core tables)

```sql
sessions        (id, driver_id, started_at, ended_at, start_loc, end_loc,
                 total_miles, gross_earnings, platform_mix)

offers          (id, session_id, platform, seen_at, displayed_payout,
                 merchant_id, dropoff_h3, stated_distance, stated_minutes,
                 offer_type, peak_pay, hit_display_cap,
                 predicted_total, predicted_minutes, verdict, action_taken)
                 -- declines are logged: they are observations, not absences

deliveries      (id, offer_id, accepted_at, merchant_arrive_at,
                 merchant_depart_at, dropoff_at, actual_payout,
                 actual_tip, actual_miles)

merchants       (id, platform_name, address, h3, geofence,
                 wait_p50_by_hour[168], wait_p90_by_hour[168], sample_n)

buildings       (id, h3_res9, label, access_notes, gate_code_encrypted,
                 entry_door, elevator_bank, lobby_handoff_ok, dock_access,
                 safe_park_geom, friction_minutes_p50, shareable)

zone_stats      (h3, hour_of_week, weather_bucket,
                 offer_arrival_rate, value_dist_params, expected_net_hourly)

parking_risk    (geom, municipality, rule_type, active_hours,
                 street_cleaning_cron, risk_score, source)

policy_runs     (id, driver_id, window_start, window_end,
                 threshold_used, realized_hourly, counterfactuals jsonb)

expenses        (id, driver_id, date, category, amount, deductible,
                 mileage_rate_applied)
```

Row-level security on every table keyed to `driver_id`. Gate codes encrypted at rest. `buildings.shareable` defaults false and only flips true for commercial and multi-unit properties on explicit opt-in.

---

## 9. UX principles

1. **One second, one glance, one color.** If the driver has to read a sentence to know what to do, it failed.
2. **Audio is the primary channel.** Visual is the fallback. The driver's eyes belong on the road.
3. **Zero-touch default path.** A full shift should be runnable without a single tap.
4. **Capture must cost under two seconds** or the knowledge base never gets built.
5. **Never show a number without showing what it's compared against.** "$38/hr" is meaningless; "$38/hr vs. $31 threshold" is a decision.
6. **Show the invisible costs prominently.** The deadhead minutes and the merchant wait are the whole reason this product exists. They should be the second thing the driver sees.

---

## 10. Roadmap

| Phase | Duration | Scope | Exit criterion |
|---|---|---|---|
| **0 — Instrument** | 2 wks | Manual logging only. Session tracker, offer logger, mileage engine (F7). No verdicts. | 300+ logged offers with outcomes. Baseline net $/hr established. |
| **1 — Read** | 3 wks | Offer card capture + parse (F1 extraction). Still no verdict — just validate that extraction is accurate. | >97% parse accuracy across 500 offers. |
| **2 — Decide** | 4 wks | Time model, tip estimator (F2), static threshold. First verdicts. | Verdict P95 <400ms. Tip MAE <$2.50. |
| **3 — Optimize** | 4 wks | Dynamic reservation rate (F6), zone map (F3), merchant oracle (F4). | Measured net $/hr lift ≥15% vs. Phase 0 baseline. |
| **4 — Compound** | 4 wks | Last-100-feet (F5), voice HUD (F12), parking shield (F11). | Time-per-delivery down ≥10% in repeat-visit buildings. |
| **5 — Multiply** | 5 wks | Multi-app (F8), weather (F9), calendar (F10). | Multi-app collision detection at zero false negatives. |
| **6 — Productize** | 6 wks | Self-experimentation (F13), shift planner (F15), onboarding, closed beta with 20 NJ drivers. | 20 beta drivers, median lift ≥15%, zero deactivations. |

Phase 0 is not optional and must not be skipped. Without a clean baseline there is no way to prove the product works, and the logged data is the training set for everything in Phase 2 onward.

---

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Screen reading deemed ToS-violating; user deactivated** | Critical | Invariants I1–I6. Manual mode ships in v1 and works standalone. Explicit informed-consent screen at enable time, in plain language, not buried in an EULA. |
| **DoorDash changes offer card layout, parsing breaks** | High | Resilient multi-strategy parsing (OCR + accessibility nodes). Parse-failure telemetry with alerting. Automatic degradation to manual mode — never emit a verdict from a low-confidence parse. |
| **Google Play rejects accessibility service usage** | High | Play policy requires strong justification for accessibility APIs used for non-accessibility purposes. Plan for direct APK distribution as primary channel. Budget legal review before submission. |
| **Cold start — model useless for first N deliveries** | Medium | Hierarchical priors from market-level data. Phase 0's 300 logged offers seed the initial model before any verdict ships. |
| **Latency budget blown by routing calls** | Medium | Aggressive matrix caching. Pre-warm routes for merchants in the current hex. Fall back to a learned travel-time model when the routing call would exceed budget. |
| **NJ reclassification changes the market structure** | Medium | If Dashers become employees, scheduling rigidity increases and the acceptance decision may disappear entirely. Mitigation: the mileage/tax engine, building intel, and route optimization retain value under either regime. Don't over-index the whole product on the accept/decline decision. |
| **The lift doesn't materialize** | Medium | Phase 3 has a hard exit criterion of ≥15% measured lift. If it isn't there, the thesis is wrong and the project stops. Build the measurement before building the optimizer. |
| **Autonomous delivery erodes the dense-urban market** | Low (3–7yr) | Not a v1 concern. Note that it threatens exactly the zones where BLACKTOP's edge is smallest anyway. |

---

## 12. Open questions

1. What is the actual displayed-payout cap value in each North/Central NJ submarket, and is it static or dynamic? Phase 0 should answer this empirically — it's the single highest-value feature in the tip model.
2. Does Priority Access materially change offer flow in sparse NJ zones? The playbook asserts yes; it's testable with an A/B over two months and it determines whether the AR-farming policy layer is worth building.
3. What is the real elasticity of the reservation rate? If the offer value distribution is thin-tailed, the optimal-stopping gain is smaller than modeled and Phase 3 fails its exit criterion.
4. Is the federated building intel network legally clean under NJ privacy law if restricted to commercial and multi-unit properties? Needs a real opinion before F14.
5. Bike/e-bike mode for Hoboken and downtown JC — completely different cost model (no fuel, no depreciation, no parking risk, no mileage deduction, weather-limited). Separate product or a mode toggle?
