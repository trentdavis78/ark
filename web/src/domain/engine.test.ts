/**
 * Engine tests, including regression guards.
 *
 * The reservation-rate assertions map to defects found by the reference
 * simulator (`core/blacktop/sim`). Each one silently degraded F6 into a fixed
 * threshold while every other unit test still passed, so they are pinned here
 * rather than left to the simulator alone.
 */

import { describe, expect, it } from "vitest";
import { hourOfWeek, isActionable, makeOffer, totalMinutes } from "./models";
import { DisplayCapDetector, TipEstimator } from "./tipEstimator";
import {
  arFarmingAdjustment, completionRisk, ReservationRateEngine, RollingOfferWindow,
  SANITY_BAND_PER_MIN, sessionEndDecay, solveWStar,
} from "./reservation";
import { MIN_EXTRACTION_CONFIDENCE, VerdictEngine } from "./verdict";
import { MerchantWaitOracle } from "./merchantOracle";
import {
  estimateTaxes, mileageRate, RATE_2026_H1, RATE_2026_H2,
  selfEmploymentTax, SessionMileageLog,
} from "./mileageTax";

const T0 = new Date("2026-08-18T18:00:00Z");
const at = (min: number) => new Date(T0.getTime() + min * 60000);

const offer = (over: Partial<Parameters<typeof makeOffer>[0]> = {}) =>
  makeOffer({
    offerId: "o1", seenAt: T0, displayedPayout: 7,
    merchantName: "Wendy's — Rt 17", merchantId: "m_wendys_17",
    statedDistanceMi: 3, statedMinutes: 20, ...over,
  });

const rate = (hourly: number) => ({
  wStarPerMin: hourly / 60, hourly, rawWPerMin: hourly / 60, decayFactor: 1,
  arAdjustmentPerMin: 0, outOfBand: false, floorApplied: false,
  arrivalRatePerMin: 0.3, sampleN: 20, notes: [],
});

describe("DisplayCapDetector", () => {
  it("stays silent until a figure repeats enough to be a ceiling", () => {
    const d = new DisplayCapDetector(5);
    for (let i = 0; i < 4; i++) d.observe(12);
    expect(d.capValue).toBeNull();
  });

  it("detects the modal repeated figure", () => {
    const d = new DisplayCapDetector(3);
    for (let i = 0; i < 6; i++) d.observe(12);
    [5.25, 7.1, 9.4].forEach((v) => d.observe(v));
    expect(d.capValue).toBe(12);
    expect(d.isAtCap(12)).toBe(true);
    expect(d.isAtCap(11.75)).toBe(false);
  });

  it("ignores non-positive payouts", () => {
    const d = new DisplayCapDetector(2);
    for (let i = 0; i < 5; i++) d.observe(0);
    expect(d.capValue).toBeNull();
  });
});

describe("TipEstimator", () => {
  it("falls back to the market prior with no history", () => {
    expect(new TipEstimator(3.5).expectedTip(offer())).toBeCloseTo(3.5);
  });

  it("lets merchant history dominate the prior", () => {
    const est = new TipEstimator(3.5, 2);
    const o = offer();
    for (let i = 0; i < 10; i++) est.observeDelivery(o, o.displayedPayout + 9);
    const tip = est.expectedTip(o);
    expect(tip).toBeGreaterThan(6);
    expect(tip).toBeLessThan(9);
  });

  it("never estimates a capped offer below an uncapped one", () => {
    const est = new TipEstimator(3.5);
    expect(est.expectedTip(offer({ displayedPayout: 12, hitDisplayCap: true })))
      .toBeGreaterThanOrEqual(est.expectedTip(offer({ displayedPayout: 12 })));
  });

  it("keeps capped outliers out of the base distribution", () => {
    const est = new TipEstimator(3.5, 1);
    const capped = offer({ displayedPayout: 12, hitDisplayCap: true, merchantId: "A" });
    for (let i = 0; i < 10; i++) est.observeDelivery(capped, 30);
    expect(est.expectedTip(offer({ merchantId: "B", merchantName: "B" })))
      .toBeCloseTo(3.5, 2);
  });

  it("counts labels from both paths", () => {
    const est = new TipEstimator();
    est.observeDelivery(offer(), 10);
    est.observeDelivery(offer({ hitDisplayCap: true }), 30);
    expect(est.labelCount).toBe(2);
  });
});

describe("RollingOfferWindow", () => {
  it("prefers fresh observations when the window has enough", () => {
    const w = new RollingOfferWindow(20, 3);
    w.observe({ seenAt: T0, valueDollars: 10, durationMinutes: 20 });
    for (let i = 0; i < 3; i++) {
      w.observe({ seenAt: at(25 + i), valueDollars: 10, durationMinutes: 20 });
    }
    const snap = w.snapshot(at(27));
    expect(snap).toHaveLength(3);
    expect(snap.every((o) => o.seenAt.getTime() > T0.getTime())).toBe(true);
  });

  it("reaches back when the window is starved", () => {
    // At high utilization a strict window holds two or three samples and the
    // optimal-stopping solve never runs.
    const w = new RollingOfferWindow(20, 4);
    for (let i = 0; i < 4; i++) {
      w.observe({ seenAt: at(i), valueDollars: 10, durationMinutes: 20 });
    }
    w.observe({ seenAt: at(90), valueDollars: 10, durationMinutes: 20 });
    expect(w.snapshot(at(92))).toHaveLength(4);
  });

  it("evicts beyond the retention horizon", () => {
    const w = new RollingOfferWindow(20, 2, 60);
    w.observe({ seenAt: T0, valueDollars: 10, durationMinutes: 20 });
    w.observe({ seenAt: at(200), valueDollars: 10, durationMinutes: 20 });
    expect(w.snapshot(at(200))).toHaveLength(1);
  });

  it("uses the real span when reaching back", () => {
    const w = new RollingOfferWindow(30, 5);
    for (let i = 0; i < 5; i++) {
      w.observe({ seenAt: at(i * 25), valueDollars: 10, durationMinutes: 20 });
    }
    expect(w.arrivalRatePerMin(at(100))).toBeCloseTo(5 / 100, 9);
  });

  it("excludes delivery time from the lambda denominator", () => {
    // Two offers over 60 elapsed minutes, 40 spent on a delivery, is 2/20 per
    // available minute — not 2/60.
    const w = new RollingOfferWindow(60, 2);
    w.observe({ seenAt: T0, valueDollars: 10, durationMinutes: 20 });
    w.observe({ seenAt: at(60), valueDollars: 10, durationMinutes: 20 });
    w.noteUnavailable(at(5), at(45));
    expect(w.arrivalRatePerMin(at(60))).toBeCloseTo(2 / 20, 9);
  });

  it("rejects an out-of-range window", () => {
    expect(() => new RollingOfferWindow(5)).toThrow(RangeError);
    expect(() => new RollingOfferWindow(90)).toThrow(RangeError);
  });
});

describe("solveWStar", () => {
  it("returns zero with nothing to go on", () => {
    expect(solveWStar([], 0.5)).toBe(0);
    expect(solveWStar([{ seenAt: T0, valueDollars: 10, durationMinutes: 20 }], 0)).toBe(0);
  });

  it("matches the closed form for a single offer type", () => {
    const obs = Array.from({ length: 10 }, (_, i) => ({
      seenAt: at(i * 2), valueDollars: 12, durationMinutes: 20,
    }));
    expect(solveWStar(obs, 0.5)).toBeCloseTo(12 / 22, 9);
  });

  it("prefers selectivity when junk is skippable", () => {
    const obs = Array.from({ length: 20 }, (_, i) => ({
      seenAt: at(i), valueDollars: i % 2 === 0 ? 4 : 20, durationMinutes: 20,
    }));
    const acceptAll = obs.reduce((s, o) => s + o.valueDollars, 0) / obs.length / 21;
    expect(solveWStar(obs, 1)).toBeGreaterThan(acceptAll);
  });
});

describe("ReservationRateEngine", () => {
  it("uses the prior at cold start", () => {
    const d = new ReservationRateEngine({ priorWPerMin: 0.5 }).currentRate(T0);
    expect(d.wStarPerMin).toBeCloseTo(0.5);
    expect(d.sampleN).toBe(0);
    expect(d.notes.some((n) => n.includes("cold start"))).toBe(true);
  });

  it("pools a thin window instead of snapping to the prior", () => {
    const eng = new ReservationRateEngine({ priorWPerMin: 0.5, minSamples: 5 });
    for (let i = 0; i < 3; i++) {
      eng.observe({ seenAt: at(i * 8), valueDollars: 6, durationMinutes: 30 });
    }
    const d = eng.currentRate(at(20));
    expect(d.wStarPerMin).toBeLessThan(0.5);
    expect(d.notes.some((n) => n.includes("thin window"))).toBe(true);
  });

  it("prefers a learned prior over the constant", () => {
    const eng = new ReservationRateEngine({ priorWPerMin: 0.5, priorProvider: () => 0.22 });
    expect(eng.currentRate(T0).wStarPerMin).toBeCloseTo(0.22);
  });

  it("flags the sanity band without overriding it", () => {
    // Clamping here would reinstate the fixed threshold F6 exists to replace.
    const eng = new ReservationRateEngine();
    for (let i = 0; i < 30; i++) {
      eng.observe({ seenAt: at(i * 0.5), valueDollars: 40, durationMinutes: 10 });
    }
    const d = eng.currentRate(at(15));
    expect(d.outOfBand).toBe(true);
    expect(d.wStarPerMin).toBeGreaterThan(SANITY_BAND_PER_MIN.hi);
  });

  it("does not floor a lean market up to the band", () => {
    const eng = new ReservationRateEngine();
    for (let i = 0; i < 30; i++) {
      eng.observe({ seenAt: at(i * 2), valueDollars: 5.4, durationMinutes: 30 });
    }
    const d = eng.currentRate(at(58));
    expect(d.wStarPerMin).toBeLessThan(SANITY_BAND_PER_MIN.lo);
    expect(d.floorApplied).toBe(false);
  });

  it("binds the marginal-cost floor", () => {
    const eng = new ReservationRateEngine({ marginalCostPerMin: 0.25 });
    for (let i = 0; i < 30; i++) {
      eng.observe({ seenAt: at(i * 2), valueDollars: 3, durationMinutes: 30 });
    }
    const d = eng.currentRate(at(58));
    expect(d.floorApplied).toBe(true);
    expect(d.wStarPerMin).toBeCloseTo(0.25);
  });

  it("keeps contexts from contaminating each other", () => {
    // One shared window let a dense corridor set a sparse zone's threshold,
    // collapsing sparse acceptance to 4%.
    const eng = new ReservationRateEngine({ minSamples: 3 });
    for (let i = 0; i < 20; i++) {
      eng.observe({ seenAt: at(i), valueDollars: 20, durationMinutes: 20 }, "dense");
      eng.observe({ seenAt: at(i), valueDollars: 5, durationMinutes: 40 }, "sparse");
    }
    const dense = eng.currentRate(at(20), null, null, "dense");
    const sparse = eng.currentRate(at(20), null, null, "sparse");
    expect(dense.wStarPerMin).toBeGreaterThan(sparse.wStarPerMin * 2);
  });

  it("decays toward the end of a session", () => {
    expect(sessionEndDecay(null)).toBe(1);
    expect(sessionEndDecay(10)).toBeCloseTo(10 / 45);
    expect(sessionEndDecay(60)).toBe(1);
  });

  it("farms acceptance rate only where it changes anything", () => {
    expect(arFarmingAdjustment(0.6, null)[0]).toBe(0);
    expect(arFarmingAdjustment(0.6, {
      currentAcceptanceRate: 0.44, targetAcceptanceRate: 0.5,
      daysUntilMonthEnd: 2, zoneIsSparse: false,
    })[0]).toBe(0);
    expect(arFarmingAdjustment(0.6, {
      currentAcceptanceRate: 0.44, targetAcceptanceRate: 0.5,
      daysUntilMonthEnd: 2, zoneIsSparse: true,
    })[0]).toBeLessThan(0);
  });
});

describe("completionRisk", () => {
  it("never fires on an open-ended session", () => {
    expect(completionRisk(90, null)).toBe(false);
  });
  it("allows the grace period", () => {
    expect(completionRisk(50, 40, 20)).toBe(false);
  });
  it("fires when the delivery cannot finish", () => {
    expect(completionRisk(70, 40, 20)).toBe(true);
  });
});

describe("VerdictEngine", () => {
  it("emits no verdict below the confidence gate", () => {
    const v = new VerdictEngine().evaluate(offer(), T0, rate(30), 0.5);
    expect(v.color).toBe("manual_fallback");
    expect(isActionable(v)).toBe(false);
    expect(v.timeBreakdown).toBeNull();
  });

  it("treats the confidence boundary as inclusive", () => {
    const v = new VerdictEngine().evaluate(offer(), T0, rate(30), MIN_EXTRACTION_CONFIDENCE);
    expect(isActionable(v)).toBe(true);
  });

  it("reads a rich offer green and a poor one red", () => {
    expect(new VerdictEngine().evaluate(offer({ displayedPayout: 22 }), T0, rate(20)).color)
      .toBe("green");
    expect(new VerdictEngine().evaluate(offer({ displayedPayout: 3 }), T0, rate(40)).color)
      .toBe("red");
  });

  it("reads the indifference band amber", () => {
    const e = new VerdictEngine();
    const o = offer({ displayedPayout: 10 });
    const hourly = (10 / totalMinutes(e.buildTimeBreakdown(o, T0))) * 60;
    expect(e.evaluate(o, T0, rate(hourly)).color).toBe("amber");
  });

  it("overrides green when the delivery cannot finish in time", () => {
    const v = new VerdictEngine().evaluate(offer({ displayedPayout: 40 }), T0, rate(10), 1, 5);
    expect(v.color).toBe("red");
    expect(v.ttsText).toContain("Won't finish");
  });

  it("lets the tip estimate move the verdict", () => {
    const o = offer({ displayedPayout: 6 });
    const without = new VerdictEngine().evaluate(o, T0, rate(25));
    const withTip = new VerdictEngine({ tipEstimator: () => 9 }).evaluate(o, T0, rate(25));
    expect(withTip.projectedNetHourly).toBeGreaterThan(without.projectedNetHourly);
    expect(withTip.expectedHiddenTip).toBeCloseTo(9);
  });

  it("floors a negative tip estimate at zero", () => {
    expect(new VerdictEngine({ tipEstimator: () => -5 })
      .evaluate(offer(), T0, rate(25)).expectedHiddenTip).toBeCloseTo(0);
  });

  it("puts the deadhead in the denominator", () => {
    const near = new VerdictEngine({ returnToDensity: () => 2 }).evaluate(offer(), T0, rate(25));
    const far = new VerdictEngine({ returnToDensity: () => 25 }).evaluate(offer(), T0, rate(25));
    expect(far.projectedNetHourly).toBeLessThan(near.projectedNetHourly);
  });

  it("falls back rather than counting a null route estimate as zero", () => {
    const tb = new VerdictEngine({
      routeProvider: {
        driveToMerchantMinutes: () => null,
        driveToCustomerMinutes: () => 12,
      },
    }).buildTimeBreakdown(offer(), T0);
    expect(tb.driveToMerchantMin).toBeGreaterThan(0);
    expect(tb.driveToCustomerMin).toBeCloseTo(12);
  });

  it("still estimates conservatively with no distance or time", () => {
    const tb = new VerdictEngine()
      .buildTimeBreakdown(offer({ statedDistanceMi: null, statedMinutes: null }), T0);
    expect(totalMinutes(tb)).toBeGreaterThan(15);
  });
});

describe("MerchantWaitOracle", () => {
  const dwell = (o: MerchantWaitOracle, id: string, minutes: number, when = T0) =>
    o.observeDwell(id, when, new Date(when.getTime() + minutes * 60000));

  it("defaults before it has evidence", () => {
    const est = new MerchantWaitOracle().waitEstimate("m1", T0);
    expect(est.source).toBe("default");
    expect(est.sampleN).toBe(0);
  });

  it("prefers the hour bucket once it is dense enough", () => {
    const o = new MerchantWaitOracle();
    for (let i = 0; i < 5; i++) dwell(o, "m1", 12);
    const est = o.waitEstimate("m1", T0);
    expect(est.source).toBe("hour");
    expect(est.p50).toBeCloseTo(12);
  });

  it("clips an implausible dwell", () => {
    const o = new MerchantWaitOracle();
    for (let i = 0; i < 5; i++) dwell(o, "m1", 500);
    expect(o.waitEstimate("m1", T0).p50).toBeLessThanOrEqual(60);
  });

  it("flags a chronic offender", () => {
    const o = new MerchantWaitOracle();
    for (let i = 0; i < 5; i++) dwell(o, "slow", 14);
    expect(o.chronicOffender("slow", T0)).toBe(true);
    expect(o.flagText("slow", T0)).toContain("14 min");
  });

  it("advises arriving later but never past the deadline", () => {
    const o = new MerchantWaitOracle();
    for (let i = 0; i < 5; i++) dwell(o, "slow", 14);
    expect(o.arrivalDelayAdvice("slow", T0, 5)).toBeCloseTo(12);
    expect(o.arrivalDelayAdvice("slow", T0, 5, 10)).toBeCloseTo(3);
    expect(o.arrivalDelayAdvice("slow", T0, 5, 0)).toBe(0);
  });

  it("rejects a departure before arrival", () => {
    expect(() => new MerchantWaitOracle().observeDwell("m", at(10), T0)).toThrow(RangeError);
  });
});

describe("mileage and tax", () => {
  it("applies the 2026 split rate", () => {
    expect(mileageRate(new Date(2026, 5, 30))).toBe(RATE_2026_H1);
    expect(mileageRate(new Date(2026, 6, 1))).toBe(RATE_2026_H2);
    expect(() => mileageRate(new Date(2025, 0, 1))).toThrow(RangeError);
  });

  it("charges each day's miles at that day's rate", () => {
    const log = new SessionMileageLog();
    const session = (d: Date, miles: number) => log.logSession({
      startTime: d, endTime: new Date(d.getTime() + 8 * 3600_000),
      startLoc: [40.8, -74.1], endLoc: [40.8, -74.1], miles, gross: 200,
    });
    session(new Date(2026, 5, 15), 100); // H1
    session(new Date(2026, 7, 15), 100); // H2
    expect(log.deduction).toBeCloseTo(100 * RATE_2026_H1 + 100 * RATE_2026_H2, 6);
  });

  it("surfaces shielded income capped at gross", () => {
    const log = new SessionMileageLog();
    log.logSession({
      startTime: new Date(2026, 7, 15), endTime: new Date(2026, 7, 15, 8),
      startLoc: [40.8, -74.1], endLoc: [40.8, -74.1], miles: 1000, gross: 100,
    });
    expect(log.deduction).toBeGreaterThan(100);
    expect(log.shieldedIncome).toBe(100);
  });

  it("rejects an invalid session", () => {
    const log = new SessionMileageLog();
    const base = {
      startTime: new Date(2026, 7, 15), endTime: new Date(2026, 7, 15),
      startLoc: [40.8, -74.1] as [number, number],
      endLoc: [40.8, -74.1] as [number, number], miles: 10, gross: 10,
    };
    expect(() => log.logSession(base)).toThrow(RangeError);
    expect(() => log.logSession({ ...base, endTime: new Date(2026, 7, 15, 8), miles: -1 }))
      .toThrow(RangeError);
  });

  it("computes SE tax on 92.35% of net profit", () => {
    expect(selfEmploymentTax(10_000)).toBeCloseTo(10_000 * 0.9235 * 0.153, 6);
    expect(selfEmploymentTax(-5)).toBe(0);
  });

  it("nets the mileage deduction out before tax", () => {
    const withDeduction = estimateTaxes(40_000, 15_000);
    const without = estimateTaxes(40_000, 0);
    expect(withDeduction.netProfit).toBe(25_000);
    expect(withDeduction.selfEmploymentTax).toBeLessThan(without.selfEmploymentTax);
    expect(withDeduction.stateIncomeTax).toBeGreaterThan(0);
  });

  it("reports true net below gross", () => {
    const log = new SessionMileageLog(
      { mpg: 28, fuelPricePerGallon: 3.4, kwhPerMile: null,
        electricityPricePerKwh: null, maintenancePerMile: 0.06, depreciationPerMile: 0.1 });
    log.logSession({
      startTime: new Date(2026, 7, 15), endTime: new Date(2026, 7, 15, 8),
      startLoc: [40.8, -74.1], endLoc: [40.8, -74.1], miles: 120, gross: 180,
    });
    expect(log.vehicleCost).toBeGreaterThan(0);
    expect(log.trueNet).toBeLessThan(180);
    expect(log.trueNetHourly(480)).toBeGreaterThan(0);
  });
});

describe("hourOfWeek", () => {
  it("covers the full week exactly once", () => {
    const start = new Date(2026, 7, 17, 0, 0); // a Monday
    const hours = new Set(
      Array.from({ length: 168 }, (_, i) =>
        hourOfWeek(new Date(start.getTime() + i * 3600_000))),
    );
    expect(hours.size).toBe(168);
  });

  it("puts Monday midnight at zero", () => {
    expect(hourOfWeek(new Date(2026, 7, 17, 0, 30))).toBe(0);
  });
});
