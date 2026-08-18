/**
 * F4 — Merchant Wait Oracle.
 *
 * Per *location*, never per brand: the same chain four miles apart behaves
 * completely differently, and averaging them destroys the only signal worth
 * having. Measured passively from geofence dwell — arrival at the merchant
 * polygon to departure — so it costs the driver no input at all.
 */

import { hourOfWeek } from "./models";

const DEFAULT_WAIT_MIN = 5;
const MAX_SAMPLES_PER_BUCKET = 40;
/** A driver who parked and took a break is not a 3-hour pickup wait. */
const MAX_PLAUSIBLE_DWELL_MIN = 60;

export interface WaitEstimate {
  p50: number;
  p90: number;
  sampleN: number;
  source: "hour" | "location" | "default";
}

function quantile(sorted: number[], q: number): number {
  if (sorted.length === 0) return 0;
  const idx = (sorted.length - 1) * q;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  const a = sorted[lo] ?? 0;
  const b = sorted[hi] ?? a;
  return lo === hi ? a : a + (b - a) * (idx - lo);
}

export class MerchantWaitOracle {
  private byHour = new Map<string, number[]>();
  private byLocation = new Map<string, number[]>();

  constructor(
    private defaultWaitMin = DEFAULT_WAIT_MIN,
    private minHourSamples = 4,
    private minLocationSamples = 3,
  ) {}

  private push(map: Map<string, number[]>, key: string, value: number, cap: number): void {
    const arr = map.get(key) ?? [];
    arr.push(value);
    if (arr.length > cap) arr.shift();
    map.set(key, arr);
  }

  observeDwell(merchantId: string, arrivedAt: Date, departedAt: Date): void {
    const minutes = (departedAt.getTime() - arrivedAt.getTime()) / 60000;
    if (minutes < 0) throw new RangeError("departure before arrival");
    const clipped = Math.min(minutes, MAX_PLAUSIBLE_DWELL_MIN);
    this.push(this.byHour, `${merchantId}|${hourOfWeek(arrivedAt)}`, clipped,
              MAX_SAMPLES_PER_BUCKET);
    this.push(this.byLocation, merchantId, clipped, MAX_SAMPLES_PER_BUCKET * 4);
  }

  waitEstimate(merchantId: string, when: Date): WaitEstimate {
    const hourly = this.byHour.get(`${merchantId}|${hourOfWeek(when)}`) ?? [];
    if (hourly.length >= this.minHourSamples) {
      const xs = [...hourly].sort((a, b) => a - b);
      return { p50: quantile(xs, 0.5), p90: quantile(xs, 0.9), sampleN: xs.length,
               source: "hour" };
    }
    const loc = this.byLocation.get(merchantId) ?? [];
    if (loc.length >= this.minLocationSamples) {
      const xs = [...loc].sort((a, b) => a - b);
      return { p50: quantile(xs, 0.5), p90: quantile(xs, 0.9), sampleN: xs.length,
               source: "location" };
    }
    return { p50: this.defaultWaitMin, p90: this.defaultWaitMin * 2, sampleN: 0,
             source: "default" };
  }

  chronicOffender(merchantId: string, when: Date, p50Threshold = 10): boolean {
    const est = this.waitEstimate(merchantId, when);
    return est.sampleN >= this.minLocationSamples && est.p50 >= p50Threshold;
  }

  flagText(merchantId: string, when: Date): string | null {
    if (!this.chronicOffender(merchantId, when)) return null;
    const est = this.waitEstimate(merchantId, when);
    return `This location runs ${Math.round(est.p50)} min at this hour, P90 ${Math.round(est.p90)}.`;
  }

  /**
   * Minutes to delay departure toward the merchant. The food will not be ready
   * any sooner, and standing at a counter is pure unpaid loss — but the delay
   * is bounded so it can never create a lateness violation.
   */
  arrivalDelayAdvice(
    merchantId: string,
    when: Date,
    driveMinutes: number,
    pickupDeadlineMinutes: number | null = null,
    counterBufferMin = 2,
  ): number {
    const est = this.waitEstimate(merchantId, when);
    let delay = Math.max(0, est.p50 - counterBufferMin);
    if (pickupDeadlineMinutes !== null) {
      const latestSafe = pickupDeadlineMinutes - driveMinutes - counterBufferMin;
      delay = Math.min(delay, Math.max(0, latestSafe));
    }
    return delay;
  }
}
