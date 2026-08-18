# BLACKTOP

A real-time accept/decline decision engine for gig delivery drivers, tuned for
North/Central New Jersey. Product spec in [`docs/PRD.md`](docs/PRD.md).

A driver makes 40–120 accept/decline calls per shift, each on a 30–45 second
timer, while driving. The offer card withholds the tip, states distance as
straight-line, and implies pickup wait is zero. The usual response is a fixed
"$2 per mile" rule applied across contexts where the correct threshold varies
by a factor of three.

BLACKTOP replaces that rule with a reservation rate solved from the driver's
own logged offers, against a time model that includes the two costs no platform
shows: how long the food actually takes, and how far the dropoff strands you
from the next offer.

```
  offer card appears in DoorDash
        │
        ├─ accessibility node traversal ──────► parse ──┐   the fast path:
        │  (text already in the a11y tree)              │   no screenshot,
        │                                               │   no network
        │  ✗ parse failed and it looked like a card     │
        └─ screenshot ──► Claude vision ──────► parse ──┤   the resilience path:
                                                        │   survives a redesign
                                                        ▼
                          confidence < 0.75 ──► no verdict, driver decides
                                                        │
                                                        ▼
                     five-term time model + hidden-tip estimate
                                                        │
                                                        ▼
                          compare vs. the dynamic reservation rate
                                                        │
                                                        ▼
                     HUD over the app: one colour, one number, spoken
```

## What's here

| Path | What it is | Verified |
|---|---|---|
| `android/domain` | The engine. Pure Kotlin, no Android dependencies, unit-tests in a plain JVM. | 60 tests |
| `android/app` | The client: capture, HUD overlay, TTS, consent, manual entry. | assembles debug + release (R8 clean) |
| `server/` | Vision extraction endpoint. The only place an API key exists. | typecheck + 10 tests |
| `core/` | Python reference implementation, plus the shift simulator that measures whether any of it works. Stdlib only. | 219 tests |
| `db/schema.sql` | Postgres + PostGIS schema with row-level security, per PRD §8. | parsed by libpg_query |
| `docs/` | PRD, architecture, compliance invariants, simulation findings. | — |

`core/` is the semantic source of truth; `android/domain` is a port of it.
Keeping both is deliberate — the simulator replays tens of thousands of shifts
to judge a policy change, which is not phone work.

## Build

The engine needs no Android SDK:

```bash
cd android
gradle :domain:test          # 60 tests
```

The app does. With `ANDROID_HOME` set (or `sdk.dir` in `local.properties`):

```bash
gradle :app:assembleDebug
gradle :app:assembleRelease  # R8-minified, ~2.3 MB
```

The vision fallback is optional and off unless you point it somewhere:

```bash
cd server && npm install && export ANTHROPIC_API_KEY=sk-ant-... && npm run dev
cd ../android
gradle :app:assembleDebug -PextractEndpoint=http://10.0.2.2:8787/api/extract
```

The measurement harness:

```bash
cd core
python3 -m pytest -q         # 219 tests
python3 -m blacktop.sim      # matched-baseline lift report
```

## On-device setup

Three grants, each explained in-app before it is requested:

1. **Accessibility** — lets the reader see the offer card. Read-only: the
   service declares `canRetrieveWindowContent` and no gesture capability, and
   is pinned to delivery app package names.
2. **Draw over other apps** — the HUD. It sets `FLAG_NOT_TOUCHABLE`, so taps
   pass through and it can never intercept your accept button.
3. **Screen capture** — optional, only for the vision fallback.

Manual entry works with none of them granted.

## Does it actually work?

The PRD makes this falsifiable — Phase 3 exits only on ≥15% measured lift — so
the simulator was built before the optimizer. It generates a North/Central NJ
market holding ground truth, reproduces the offer card's three distortions, and
replays the identical offer stream against both policies.

Over a 20-day matched season after a 5-day Phase 0 warm-up:

| zone | baseline $2/mi | BLACKTOP | lift | share of achievable gain |
|---|---|---|---|---|
| Paramus / Rt 17 | $10.27/hr | $12.14/hr | **+18.3%** | 88% |
| Morristown / Chatham | $14.73/hr | $15.29/hr | **+3.8%** | 68% |
| Hunterdon fringe | $4.93/hr | $6.36/hr | **+29.1%** | 55% |
| **overall** | **$9.98/hr** | **$11.27/hr** | **+12.9%** | — |

Net dollars per *online* hour, after modelled vehicle cost and tax reserve.
**+12.9% is below the PRD's +20% goal and below the +15% Phase 3 gate.** The
aggregate is held down by the suburban zone, where a clairvoyant threshold
gains only $0.82/hr over the naive rule — high-tip areas reward $2/mi nearly as
well as they reward anything.

Running that harness also found three defects in the reservation rate, each of
which silently collapsed it back into a fixed threshold while every unit test
passed. [`docs/SIMULATION.md`](docs/SIMULATION.md) has the details.

## The risk you are taking

Reading the offer card on your own device is not something any delivery
platform has explicitly permitted. It touches no platform server and uses no
credentials, which makes it materially different from tools that have been shut
down — but it is not blessed either. The app says so in plain language before
anything is enabled, and manual entry exists so the product still works for a
driver who declines. [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) states each
invariant and where the code holds it.

Distribution likely means a direct APK rather than Play Store: accessibility
services used for non-accessibility purposes need strong justification, and
PRD §11 budgets for that fight.

## Not built

F3 (zone map), F5 (building intel), and F13 (counterfactual replay) exist in
`core/` and run in the simulator but are not wired into the app. F8–F11
(multi-app, weather, calendar, parking shield) are not implemented.
`db/schema.sql` is written and parse-verified, but nothing syncs to it — the
app is local-only, logging offers to app-private storage.

## Not tax advice

`core/blacktop/mileage_tax.py` applies the 2026 split rate (72.5¢/mi through
June 30, 76¢ from July 1) and estimates SE, federal, and NJ tax for planning.
It is not a substitute for an accountant.
