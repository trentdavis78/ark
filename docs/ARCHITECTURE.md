# Architecture

Maps this repository to the system in [`PRD.md`](PRD.md) §7.

## Flow

```
  offer card drawn by the delivery app
        │
        ▼
  OfferCaptureService                      android/app/.../capture/
    accessibility node traversal, read-only
        │
        │  usable parse ────────────────────────────┐   fast path
        │                                           │   no image, no network
        │  parse failed AND the text looked like    │
        │  an offer card (≥2 markers)               │
        ▼                                           │
  VisionFallback                                    │   resilience path
    MediaProjection frame ──► POST /api/extract     │   seconds, not ms
    claude-opus-5, output constrained to a schema   │   survives a redesign
        │                                           │
        └───────────────────────────────────────────┤
                                                    ▼
                          confidence < 0.75 ──► MANUAL_FALLBACK
                                                    │   no number is shown
                                                    ▼
  VerdictEngine                            android/domain/.../VerdictEngine.kt
    five-term time model:
      drive to merchant + merchant wait (F4)
        + drive to customer + dropoff friction
        + return to density  ← the deadhead nobody models
    value model: displayed + E[hidden tip] (F2)
        │
        ▼
  ReservationRateEngine                    android/domain/.../ReservationRate.kt
    w*(now, context) from optimal stopping
        │
        ▼
  HudService                               android/app/.../hud/HudService.kt
    overlay + TTS. FLAG_NOT_TOUCHABLE — taps pass through.
```

Everything after capture is local and works offline. Only the fallback needs
network, and it is off unless the driver enables it.

## Why two capture paths

They fail differently, which is the whole reason to have both.

**Node traversal** is fast and private — the text is already in the
accessibility tree, so there is no screenshot, no OCR, and no round trip. It is
the only path that fits PRD §4's sub-400ms budget. It is also brittle: it
depends on regexes over text whose layout DoorDash controls and will change.

**Vision** is slow and needs network, but it reads the card by looking at it,
which is what makes it immune to the layout change that breaks the parser. PRD
§11 lists that change as a certainty rather than a risk, and this is the answer
to it.

So the fallback only fires when node parsing has already failed *and* the text
carried at least two offer-card markers. Most window updates are not offers,
and treating them as parse failures would bury a real layout change in noise.
It is rate-limited to one attempt per 20 seconds, because a broken layout fires
failures continuously and one screenshot per offer timer is the most that could
possibly help.

## Repository map

| Path | Role | Verified |
|---|---|---|
| `android/domain/` | The engine: models, tip estimator, wait oracle, reservation rate, verdict, parser. No Android imports. | 60 tests in a plain JVM |
| `android/app/capture/` | `OfferCaptureService` (read-only node traversal), `ScreenCapture` (MediaProjection), `VisionFallback`. | compiles; debug + release assemble |
| `android/app/hud/` | The overlay and TTS. | as above |
| `android/app/data/` | Session state, the offer log, parse-failure telemetry. | as above |
| `android/app/ui/` | Consent screen, setup, manual entry. | as above |
| `server/` | The vision endpoint. The only place an API key exists. | typecheck + 10 tests |
| `core/blacktop/` | Python reference implementation. Semantic source of truth. | 219 tests |
| `core/blacktop/sim/` | Shift simulator: generative NJ market, matched-baseline runner, clairvoyant ceiling. | included above |
| `db/schema.sql` | Postgres + PostGIS + RLS, per PRD §8. Not yet wired to the app. | parsed by libpg_query |

## Why the engine is its own module

`:domain` has no Android dependencies, so `gradle :domain:test` runs on a bare
machine with no SDK, emulator, or device. The math that decides a driver's
income should be testable in a plain JVM, and the module boundary is what keeps
it that way — an accidental `android.*` import fails the build rather than
quietly making the engine untestable.

`allWarningsAsErrors` is on for that module specifically. Most of its warnings
would be a nullable route estimate or an absent card field being widened into a
default, and those are exactly the bugs that produce a confident wrong number.

## Why there are two implementations

`core/` is Python and `android/domain` is Kotlin, implementing the same
algorithms. The duplication is deliberate and bounded.

The simulator has to replay tens of thousands of shifts to say anything useful
about a policy change, and it needs to be trivially scriptable. So `core/` is
where a change to the decision math gets *judged* — the zone-level lift
assertions in `core/tests/test_sim.py` catch regressions that unit tests miss —
and `android/domain` is the port that ships.

The risk is drift. The mitigation is that both sides carry the same tests for
the same properties, with the same names, so an unmirrored change shows up as a
missing test rather than a silent divergence.

## The reservation rate

`android/domain/.../ReservationRate.kt` and `core/blacktop/reservation.py` are
the subtlest part of the system, and four of their properties are load-bearing.
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

`db/schema.sql` exists but nothing syncs to it — the app logs offers to
app-private newline-delimited JSON, excluded from backup. F3, F5, and F13 exist
in `core/` and are exercised by the simulator but are not wired into the app.
F8–F11 are not implemented.
