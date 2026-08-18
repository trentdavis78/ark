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
screenshot the offer card
        │
        ▼  share sheet / drop / paste
  Claude reads the fields                 web/src/server/extract.ts
        │  confidence < 0.75 ──► no verdict, driver decides
        ▼
  five-term time model + hidden-tip estimate    web/src/domain/verdict.ts
        │
        ▼
  compare against the dynamic reservation rate  web/src/domain/reservation.ts
        │
        ▼
  TAKE IT · CLOSE · SKIP   + spoken           web/src/app/main.ts
```

## What's here

| Path | What it is | Verified |
|---|---|---|
| `web/` | The app: an installable, offline-first PWA. TypeScript, no UI framework. | 73 tests, typecheck, production build |
| `core/` | Python reference implementation of every algorithm, plus the shift simulator that measures whether any of it works. Stdlib only. | 219 tests |
| `db/schema.sql` | Postgres + PostGIS schema with row-level security, per PRD §8. | Parsed against the real PostgreSQL grammar |
| `docs/` | PRD, architecture, compliance invariants, simulation findings. | — |

The Python core is the semantic source of truth; `web/src/domain` is a port of
it. Keeping both is deliberate — the simulator needs to run tens of thousands
of shifts offline, and that is not something to do in a browser.

## Run it

```bash
cd web
npm install
export ANTHROPIC_API_KEY=sk-ant-...   # server-side only; see web/.env.example
npm run dev                           # http://localhost:5173
npm test                              # 73 tests
npm run build
```

Install it to the home screen on Android to register BLACKTOP in the share
sheet. Then the loop is: screenshot the offer → share → verdict, spoken.

The measurement harness:

```bash
cd core
python3 -m pytest -q                  # 219 tests
python3 -m blacktop.sim               # matched-baseline lift report
```

## Does it actually work?

The PRD makes this falsifiable — Phase 3 exits only on ≥15% measured lift — so
the simulator was built before the optimizer. It generates a North/Central NJ
market that holds ground truth, reproduces the offer card's three distortions,
and replays the identical offer stream against both policies.

Over a 20-day matched season after a 5-day Phase 0 warm-up:

| zone | baseline $2/mi | BLACKTOP | lift | share of the achievable gain |
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

## What this version does not do

The PRD specifies an Android app that reads the offer card off the screen
automatically and floats a verdict over it. A PWA cannot do either: it cannot
observe another app's rendered content, and it cannot draw over one. So capture
is a screenshot the driver shares, not a silent read, and the verdict appears
in the app rather than on top of DoorDash.

That costs the sub-400ms budget in PRD §4 — a vision round trip is seconds, not
milliseconds, though it fits inside a 30–45 second timer. What it buys is the
compliance position: no accessibility service, no screen reading, no overlay,
and therefore none of the gray zone PRD §5.1 identifies as the product's
largest risk. See [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md).

Also not built: the zone map (F3), building intel (F5), multi-app (F8), weather
(F9), calendar (F10), parking shield (F11). F3, F4, F5, F7, and F13 exist in
`core/` and are exercised by the simulator, but only F1, F2, F4, F6, and F7 are
wired into the app.

## Not tax advice

`web/src/domain/mileageTax.ts` applies the 2026 split rate (72.5¢/mi through
June 30, 76¢ from July 1) and estimates SE, federal, and NJ tax for planning.
It is not a substitute for an accountant.
