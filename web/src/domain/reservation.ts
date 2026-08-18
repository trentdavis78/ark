/**
 * F6 — Dynamic Reservation Rate.
 *
 * The threshold F1 compares against. Not a constant and not a user setting:
 * offers arrive from a time- and location-varying distribution and each must
 * be decided in isolation, so this is an optimal-stopping problem. The optimal
 * policy is a reservation rate `w*` at the indifference point — accept exactly
 * when an offer's value rate beats the expected value of declining and
 * continuing to search.
 *
 * Four properties below are load-bearing. Each was established by measuring a
 * policy that lacked it against a matched baseline, and each failure mode was
 * the same: the engine silently degenerating into a fixed threshold. See
 * `docs/SIMULATION.md`; the reference simulator lives in `core/blacktop/sim`.
 *
 *   1. The sanity band is a diagnostic, never a clamp.
 *   2. The window survives starvation and pools toward a learned prior.
 *   3. Windows are keyed by context; one market must not set another's rate.
 *   4. Lambda counts minutes *available*, not minutes elapsed.
 */

/** PRD F6: where w* is expected to land. Leaving it signals an upstream bug. */
export const SANITY_BAND_PER_MIN = { lo: 0.30, hi: 0.85 } as const;

export const DEFAULT_WINDOW_MINUTES = 30;
export const DEFAULT_DECAY_HORIZON_MINUTES = 45;
export const MIN_AVAILABLE_MINUTES = 1;

/** One observed offer — accepted or declined. Both are observations. */
export interface OfferObservation {
  seenAt: Date;
  valueDollars: number;
  durationMinutes: number;
}

export const ratePerMin = (o: OfferObservation): number =>
  o.durationMinutes <= 0 ? 0 : o.valueDollars / o.durationMinutes;

/** Acceptance-rate targeting inputs (PRD F6 policy constraints). */
export interface ArPolicyInputs {
  currentAcceptanceRate: number;
  targetAcceptanceRate: number;
  daysUntilMonthEnd: number;
  /** AR only matters where Priority Access actually changes offer flow. */
  zoneIsSparse: boolean;
}

/** The rate with its full derivation, so the UI can show its work. */
export interface RateDecision {
  wStarPerMin: number;
  hourly: number;
  rawWPerMin: number;
  decayFactor: number;
  arAdjustmentPerMin: number;
  outOfBand: boolean;
  floorApplied: boolean;
  arrivalRatePerMin: number;
  sampleN: number;
  notes: string[];
}

const minutesBetween = (a: Date, b: Date): number =>
  (b.getTime() - a.getTime()) / 60000;

/**
 * Rolling estimate of the offer distribution and arrival rate for one context.
 *
 * A working driver only *sees* offers while idle, so a strict 20–40 minute
 * wall window holds two or three observations at high utilization and the
 * optimal-stopping solve never has enough data to run. Falling back to a
 * constant in that state turns the engine back into the fixed threshold it
 * exists to replace, so the window is bounded by time *and* by sample count:
 * it reaches further back when the nominal window is thin, and reports the
 * span it actually covered.
 */
export class RollingOfferWindow {
  private observations: OfferObservation[] = [];
  private unavailable: Array<[Date, Date]> = [];
  private retain: number;

  constructor(
    private windowMinutes = DEFAULT_WINDOW_MINUTES,
    private minSamples = 5,
    retainMinutes = 240,
  ) {
    if (!(windowMinutes >= 15 && windowMinutes <= 60)) {
      throw new RangeError("windowMinutes should be in [15, 60]");
    }
    this.retain = Math.max(retainMinutes, windowMinutes);
  }

  observe(obs: OfferObservation): void {
    this.observations.push(obs);
    this.evict(obs.seenAt);
  }

  /**
   * Mark a stretch spent on a delivery, when no offer could have arrived.
   * This is the driver's own session timeline, not platform data (I5).
   */
  noteUnavailable(start: Date, end: Date): void {
    if (end.getTime() > start.getTime()) this.unavailable.push([start, end]);
  }

  /** Drop past the retention horizon — never merely past the window, since the
   *  older history is what rescues a starved one. */
  private evict(now: Date): void {
    const cutoff = now.getTime() - this.retain * 60000;
    this.observations = this.observations.filter((o) => o.seenAt.getTime() >= cutoff);
    this.unavailable = this.unavailable.filter(([, end]) => end.getTime() >= cutoff);
  }

  snapshot(now: Date): OfferObservation[] {
    this.evict(now);
    const cutoff = now.getTime() - this.windowMinutes * 60000;
    const fresh = this.observations.filter((o) => o.seenAt.getTime() >= cutoff);
    if (fresh.length >= this.minSamples) return fresh;
    return this.observations.slice(-this.minSamples);
  }

  /** Start of the period `snapshot` actually covers. */
  private spanStart(now: Date): Date {
    const obs = this.snapshot(now);
    const nominal = new Date(now.getTime() - this.windowMinutes * 60000);
    const first = obs[0];
    if (obs.length < 2 || !first) return nominal;
    return first.seenAt.getTime() < nominal.getTime() ? first.seenAt : nominal;
  }

  spanMinutes(now: Date): number {
    return Math.max(minutesBetween(this.spanStart(now), now), MIN_AVAILABLE_MINUTES);
  }

  /** Elapsed minutes in the span minus time spent on deliveries. */
  availableMinutes(now: Date): number {
    const start = this.spanStart(now);
    let busy = 0;
    for (const [b0, b1] of this.unavailable) {
      const lo = Math.max(b0.getTime(), start.getTime());
      const hi = Math.min(b1.getTime(), now.getTime());
      if (hi > lo) busy += (hi - lo) / 60000;
    }
    return Math.max(minutesBetween(start, now) - busy, MIN_AVAILABLE_MINUTES);
  }

  /**
   * Offers seen per minute *available*.
   *
   * Dividing by elapsed time instead ignores that offers arriving mid-delivery
   * are never seen. That understates lambda, lengthens the modelled wait for a
   * better draw, depresses w*, and makes the engine accept offers it should
   * decline — worth roughly 15 points of excess acceptance in every simulated
   * zone before this correction.
   */
  arrivalRatePerMin(now: Date): number {
    const obs = this.snapshot(now);
    if (obs.length === 0) return 0;
    return obs.length / this.availableMinutes(now);
  }

  get size(): number {
    return this.observations.length;
  }
}

/**
 * Solve the optimal-stopping fixed point on the empirical distribution.
 *
 * Scans a threshold at each observed rate and returns the earning rate at the
 * maximizing one, which equals w* at the indifference point. Returns 0 when
 * there is nothing to go on, and the caller falls back to its prior.
 */
export function solveWStar(
  observations: OfferObservation[],
  arrivalRatePerMin: number,
): number {
  const obs = observations.filter((o) => o.durationMinutes > 0);
  if (obs.length === 0 || arrivalRatePerMin <= 0) return 0;
  const thresholds = [...new Set(obs.map(ratePerMin))].sort((a, b) => a - b);
  let best = 0;
  for (const w of thresholds) {
    const accepted = obs.filter((o) => ratePerMin(o) >= w);
    if (accepted.length === 0) continue;
    const p = accepted.length / obs.length;
    const meanValue = accepted.reduce((s, o) => s + o.valueDollars, 0) / accepted.length;
    const meanDuration =
      accepted.reduce((s, o) => s + o.durationMinutes, 0) / accepted.length;
    const expectedWait = 1 / (arrivalRatePerMin * p);
    best = Math.max(best, meanValue / (expectedWait + meanDuration));
  }
  return best;
}

/**
 * Inside the final stretch of a planned session the rate decays toward the
 * floor: there is no future left to wait for. `null` (open-ended) means 1.
 */
export function sessionEndDecay(
  remainingMinutes: number | null,
  horizonMinutes = DEFAULT_DECAY_HORIZON_MINUTES,
): number {
  if (remainingMinutes === null) return 1;
  if (remainingMinutes >= horizonMinutes) return 1;
  return Math.max(0, remainingMinutes / horizonMinutes);
}

/**
 * Temporarily lower w* to farm acceptance rate for Priority Access — but only
 * where it changes anything: sparse zones, near month end, AR below target.
 */
export function arFarmingAdjustment(
  wPerMin: number,
  ar: ArPolicyInputs | null,
): [number, string | null] {
  if (!ar || !ar.zoneIsSparse) return [0, null];
  const deficit = ar.targetAcceptanceRate - ar.currentAcceptanceRate;
  if (deficit <= 0 || ar.daysUntilMonthEnd > 7) return [0, null];
  const scale = Math.min(1, deficit / 0.1);
  const adj = -Math.min(0.25 * wPerMin, 0.25 * wPerMin * scale);
  return [
    adj,
    `AR farming: ${Math.round(ar.currentAcceptanceRate * 100)}% vs target ` +
      `${Math.round(ar.targetAcceptanceRate * 100)}%, ${ar.daysUntilMonthEnd}d left in month`,
  ];
}

/**
 * Guardrail: true when accepting likely cannot finish inside the planned
 * session. F1 downgrades any green to red when this fires, because completion
 * rate ≥ 95% is release-blocking (PRD §4).
 */
export function completionRisk(
  projectedMinutes: number,
  remainingSessionMinutes: number | null,
  graceMinutes = 20,
): boolean {
  if (remainingSessionMinutes === null) return false;
  return projectedMinutes > remainingSessionMinutes + graceMinutes;
}

export interface ReservationOptions {
  windowMinutes?: number;
  priorWPerMin?: number;
  minSamples?: number;
  shrinkageK?: number;
  /** Never recommend an offer that fails to clear the cost of driving it. */
  marginalCostPerMin?: number;
  /** Context prior learned from the driver's own history (hex × hour × weather). */
  priorProvider?: (now: Date) => number | null;
}

/**
 * Maintains the per-context windows and produces the layered decision.
 *
 * PRD F6 scopes the estimate to "the current hex cluster × hour × weather
 * bucket", and that scoping is not decorative: one shared window lets a dense
 * corridor's distribution set a sparse zone's threshold, and the sparse zone
 * then declines nearly everything it is offered. Hour is left to recency,
 * since a 20–40 minute window sits inside one hour by construction.
 */
export class ReservationRateEngine {
  private windows = new Map<string, RollingOfferWindow>();
  private readonly windowMinutes: number;
  private readonly priorWPerMin: number;
  private readonly minSamples: number;
  private readonly shrinkageK: number;
  private readonly marginalCostPerMin: number;
  private readonly priorProvider: ((now: Date) => number | null) | null;

  constructor(opts: ReservationOptions = {}) {
    this.windowMinutes = opts.windowMinutes ?? DEFAULT_WINDOW_MINUTES;
    this.priorWPerMin = opts.priorWPerMin ?? 0.5;
    this.minSamples = opts.minSamples ?? 5;
    this.shrinkageK = opts.shrinkageK ?? 4;
    this.marginalCostPerMin = opts.marginalCostPerMin ?? 0;
    this.priorProvider = opts.priorProvider ?? null;
  }

  private windowFor(context: string): RollingOfferWindow {
    let w = this.windows.get(context);
    if (!w) {
      w = new RollingOfferWindow(this.windowMinutes, this.minSamples);
      this.windows.set(context, w);
    }
    return w;
  }

  observe(obs: OfferObservation, context = ""): void {
    this.windowFor(context).observe(obs);
  }

  noteUnavailable(start: Date, end: Date, context = ""): void {
    this.windowFor(context).noteUnavailable(start, end);
  }

  private priorFor(now: Date): number {
    const p = this.priorProvider?.(now);
    return p !== null && p !== undefined && p > 0 ? p : this.priorWPerMin;
  }

  currentRate(
    now: Date,
    remainingSessionMinutes: number | null = null,
    ar: ArPolicyInputs | null = null,
    context = "",
  ): RateDecision {
    const window = this.windowFor(context);
    const obs = window.snapshot(now);
    const lambda = window.arrivalRatePerMin(now);
    const notes: string[] = [];

    const prior = this.priorFor(now);
    const solved = obs.length >= 2 ? solveWStar(obs, lambda) : 0;
    const n = obs.length;
    let raw: number;
    if (solved <= 0) {
      raw = prior;
      notes.push(`cold start: ${n} usable samples, using prior ${prior.toFixed(3)}/min`);
    } else {
      // Partial pooling, the same shrinkage F2 uses. A hard cold-start switch
      // to a constant is what starved the rate in practice; pooling degrades
      // smoothly and washes out as data arrives.
      raw = (n * solved + this.shrinkageK * prior) / (n + this.shrinkageK);
      if (n < this.minSamples) {
        notes.push(
          `thin window: ${n} samples, pooled ${solved.toFixed(3)} toward prior ${prior.toFixed(3)}`,
        );
      }
    }

    const decay = sessionEndDecay(remainingSessionMinutes);
    if (decay < 1) notes.push(`session-end decay x${decay.toFixed(2)}`);
    let w = raw * decay;

    const [arAdj, arNote] = arFarmingAdjustment(w, ar);
    if (arNote) notes.push(arNote);
    w += arAdj;

    const floorApplied = w < this.marginalCostPerMin;
    if (floorApplied) {
      notes.push(
        `marginal-cost floor: ${w.toFixed(3)} -> ${this.marginalCostPerMin.toFixed(3)}/min`,
      );
      w = this.marginalCostPerMin;
    }

    // Diagnostic only. Clamping to the band edge would silently reinstate a
    // fixed threshold, and in a lean market it declines every offer available.
    const outOfBand = w < SANITY_BAND_PER_MIN.lo || w > SANITY_BAND_PER_MIN.hi;
    if (outOfBand) {
      notes.push(
        `outside sanity band: ${w.toFixed(3)}/min not in ` +
          `[${SANITY_BAND_PER_MIN.lo}, ${SANITY_BAND_PER_MIN.hi}] — check upstream`,
      );
    }

    return {
      wStarPerMin: w,
      hourly: w * 60,
      rawWPerMin: raw,
      decayFactor: decay,
      arAdjustmentPerMin: arAdj,
      outOfBand,
      floorApplied,
      arrivalRatePerMin: lambda,
      sampleN: n,
      notes,
    };
  }
}
