# Measuring the thesis

PRD §11 lists "the lift doesn't materialize" as a live risk and gives Phase 3 a
hard exit criterion of ≥15% measured lift. So the measurement was built first,
before any work on the optimizer it was meant to judge.

## The harness

`core/blacktop/sim` generates a North/Central NJ market that holds ground truth
the engine never sees. It reproduces the three distortions PRD §1 identifies in
the real offer card:

- the tip is withheld above a market display cap,
- travel is stated as straight-line distance, not road distance,
- pickup wait is stated as zero.

Three zones stand in for the archetypes the NJ playbook describes: a dense
low-tip corridor, an affluent suburban pocket, and a sparse long-haul fringe.
Merchants carry per-location wait distributions, because the same chain four
miles apart behaves completely differently.

Both policies replay the **identical** generated stream, so the only thing that
differs is what they do with it. A busy driver does not see offers that arrive
mid-delivery — that opportunity cost is the entire reason an acceptance
threshold matters, and it is what a per-offer $/mi rule cannot reason about.

The incumbent is the static $2-per-mile rule on the card's own numbers. Five
warm-up days reproduce PRD Phase 0: the old heuristic drives while BLACKTOP
logs and learns but issues no verdicts.

A third policy, `OraclePolicy`, accepts on the *realized* value rate at a fixed
threshold chosen with hindsight. It is not a competitor but a ceiling, and it
answers the question a bare lift percentage cannot: when the engine
underperforms, is that an estimation problem or was there no gain available?

## Result

20 measured days × 8 hours × 3 zones, after 5 Phase 0 days:

| zone | baseline | BLACKTOP | lift | ceiling | capture |
|---|---|---|---|---|---|
| Paramus / Rt 17 | $10.27 | $12.14 | +18.3% | $12.40 | 88% |
| Morristown / Chatham | $14.73 | $15.29 | +3.8% | $15.55 | 68% |
| Hunterdon fringe | $4.93 | $6.36 | +29.1% | $7.52 | 55% |
| **overall** | **$9.98** | **$11.27** | **+12.9%** | — | — |

Net dollars per online hour, after modelled vehicle cost and tax reserve.

**+12.9% misses both the +20% goal and the +15% Phase 3 gate.** The honest
reading: the suburban zone holds the aggregate down, and it does so for a real
reason rather than an implementation one. A perfect threshold gains only
$0.82/hr there, because when most offers carry a large tip, a crude rule
accepts most of them and is right to. The engine's edge concentrates exactly
where the PRD predicts — the dense corridor where offer quality varies wildly,
and the sparse fringe where deadhead dominates and the naive rule is blind to
it.

Note the direction of the sparse-zone correction. BLACKTOP accepts **more**
there than the baseline (49% vs 36%), not less: in a thin market you cannot
afford to be picky, and the $2/mi rule was wrongly rejecting long hauls that
were fine. A fixed threshold cannot express that, which is the whole argument
for a dynamic one.

## Three defects the harness found

Each of these silently degraded the dynamic reservation rate into a fixed
threshold — the exact failure the feature exists to prevent — while every unit
test still passed. All three showed up as a negative lift in at least one zone,
which is why the zone-level assertions in `core/tests/test_sim.py` are the
regression guards rather than the aggregate.

**1. The sanity band was applied as a clamp.** PRD F6 states the $0.30–$0.85
band as a diagnostic: "if it leaves that band, something is wrong upstream."
The implementation clamped `w*` into it. In a market where the achievable rate
sits below $0.30/min, that pins the threshold above every available offer and
declines everything. The band now flags and leaves `w*` alone; the only floor
is marginal driving cost, which is the one economically correct floor.

**2. The rolling window starved.** A driver only sees offers while idle, so at
75% utilization a 30-minute window held about three samples — under
`min_samples` — and `w*` fell back to a hardcoded $0.50/min prior on 31 of 42
decisions, roughly double what the market supported. The window now reaches
back for a minimum sample count and pools toward a learned context prior
instead of switching to a constant.

**3. Threshold estimation was global.** PRD F6 scopes the estimate to hex
cluster × hour × weather. One shared window let the dense corridor set the
sparse zone's threshold; sparse acceptance fell to 4% and earnings to $1.45/hr.
Windows are now keyed by context.

A fourth correction came from comparing acceptance against the ceiling rather
than from a crash. BLACKTOP was accepting ~15 points more than optimal in every
zone, traced to λ being computed per minute *elapsed*. Offers arriving
mid-delivery are never seen, so that understates arrival rate, lengthens the
modelled wait for a better draw, and depresses the threshold. Counting minutes
*available* instead moved dense-zone capture from 69% to 88%.

## Reproducing

```bash
cd core
python3 -m blacktop.sim                      # default 20 days, with ceiling
python3 -m blacktop.sim --days 40 --no-oracle
python3 -m pytest -q                         # 219 tests
```

The simulator is seeded, so a given seed always produces the same market.

## What it does not establish

This is a model, and it is calibrated to published driver economics rather than
to measured NJ data — which is precisely what PRD Phase 0 exists to collect. It
supports a claim about *mechanism*: given a market with these properties, a
dynamic reservation rate over a five-term time model beats a fixed $/mi rule,
and it does so through deadhead and merchant wait. It does not establish the
magnitude for any real driver. Only Phase 0 can, and if the lift is not there
against a real baseline, PRD §11 is unambiguous about what that means.
