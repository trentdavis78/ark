# Phase 0 — the field protocol

PRD §10 is unambiguous: Phase 0 is not optional and must not be skipped.
Without a clean baseline there is no way to prove the product works, and the
logged offers are the training set for everything from Phase 2 onward.

Right now every number BLACKTOP produces is simulated. This is how that
changes.

## Before the first shift: does the reader work at all?

The parser's regexes were written against the PRD's description of a DoorDash
offer card, not against a real accessibility tree. This is the single largest
unknown in the codebase, and it takes about ten minutes to settle.

1. Install the debug APK: `adb install app/build/outputs/apk/debug/app-debug.apk`
   (or sideload it — it is signed with the Android debug certificate).
2. Open BLACKTOP, read the consent screen, tap through.
3. Enable the accessibility service when it prompts. Nothing else yet — no
   overlay, no screen capture, and do **not** go online.
4. Open **Reader diagnostics** and turn on *Record captures*.
5. Switch to DoorDash. Wait for one offer to appear. Let it expire; do not
   accept.
6. Come back to BLACKTOP → Reader diagnostics.

Three things can happen, and each is informative:

| What you see | What it means | What to do |
|---|---|---|
| **PARSED**, fields populated | Node capture works. The fast path is live. | Go to the baseline week. |
| **FAILED**, but raw text is present | The tree gives us the card, the regexes are wrong. | Share the report. This is a small, precise fix. |
| Nothing captured, or raw text empty | The card is drawn without text nodes, or events are not arriving. | Share the report. The vision fallback becomes the primary path. |

The third case is the one worth knowing before you build a week around this.
It is also survivable — that is exactly why the vision fallback exists.

**The report contains the text that was on your screen**, including the dropoff
area. Read it before sending it anywhere.

## Week one: the baseline

The point of Phase 0 is a *matched* baseline. That means driving the way you
already drive, while BLACKTOP watches. Do not act on its verdicts yet.

- Run your normal heuristic. If that is "$2 a mile", keep doing it exactly.
- Let the app log every offer it sees, accepted and declined alike. A declined
  offer is an observation about that hour and place, not an absence — it is
  what makes the reservation rate solvable at all.
- Record what you actually did on each offer, so the log knows your effective
  threshold.
- Note your gross and your online hours per shift independently. That is the
  denominator everything is measured against.

Target: **300+ logged offers with outcomes**, which is roughly one week of
full shifts. PRD §10 sets that as the Phase 0 exit criterion, and it is also
about where the tip model stops being a prior and starts being a model.

## What week one answers

**Open question #1 — the display cap.** PRD §12 calls this "the single
highest-value feature in the tip model", and it is answerable by counting. If
offers keep landing on the identical dollar figure, that figure is your
market's ceiling. The app detects it automatically; check whether it has found
one after a few hundred offers.

**Is the parser reliable?** Watch the parse-failure counter on the main screen.
It surfaces a warning when failures pile up, because a layout change should be
visible rather than silently degrading accuracy.

**What is your real baseline?** Net dollars per online hour, after true vehicle
cost and tax reserve. Not gross, not per delivery, not per mile.

## Then, and only then

Turn on verdicts and measure the lift against that baseline. PRD §10 gives
Phase 3 a hard exit criterion of **≥15%**, and PRD §11 is blunt about what a
miss means: "If it isn't there, the thesis is wrong and the project stops."

The simulator says +12.9% — below that gate, with the shortfall concentrated in
high-tip suburban zones where a crude rule already does well. Treat that as a
prediction to be tested, not a result. See [`SIMULATION.md`](SIMULATION.md).

## Guardrails while you do this

These are release-blocking in PRD §4 and worth watching from day one:

- Completion rate ≥ 95%
- Customer rating ≥ 4.7
- Zero contract violations
- **Zero deactivations**

If any of them moves, stop and reassess before continuing. A feature that lifts
hourly but degrades completion rate ships disabled.
