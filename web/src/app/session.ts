/**
 * Session state — the live wiring between the vision reader, the engine, and
 * everything the driver's own history has taught it.
 *
 * One object owns the learned models so a verdict, a completed delivery, and
 * the running net-per-hour display all read the same state.
 */

import type { Offer, Verdict, WeatherBucket } from "../domain/models";
import { totalMinutes } from "../domain/models";
import { TipEstimator } from "../domain/tipEstimator";
import { MerchantWaitOracle } from "../domain/merchantOracle";
import { ReservationRateEngine } from "../domain/reservation";
import type { RateDecision } from "../domain/reservation";
import { VerdictEngine } from "../domain/verdict";
import {
  costPerMile, DEFAULT_VEHICLE, SessionMileageLog,
} from "../domain/mileageTax";
import type { VehicleCostParams } from "../domain/mileageTax";
import { allOffers, putOffer, updateOffer } from "./store";
import type { StoredOffer } from "./store";

/** Blended suburban-NJ average, used to convert cost/mile into cost/minute. */
const BLENDED_MPH = 20;

export class RunningMean {
  n = 0;
  mean = 0;
  add(x: number): void {
    this.n += 1;
    this.mean += (x - this.mean) / this.n;
  }
}

export class DriverSession {
  readonly tips = new TipEstimator();
  readonly waits = new MerchantWaitOracle();
  readonly rates: ReservationRateEngine;
  readonly mileage: SessionMileageLog;
  private engine: VerdictEngine;

  /** Learned from the driver's own completed deliveries. */
  private deadheadByArea = new Map<string, RunningMean>();
  private globalRate = new RunningMean();
  private currentArea = "";

  startedAt: Date | null = null;
  plannedEndAt: Date | null = null;
  weather: WeatherBucket = "clear";
  offersSeen = 0;
  offersAccepted = 0;

  constructor(vehicle: VehicleCostParams = DEFAULT_VEHICLE) {
    this.mileage = new SessionMileageLog(vehicle);
    this.rates = new ReservationRateEngine({
      // Never recommend an offer that fails to clear the cost of driving it.
      marginalCostPerMin: (costPerMile(vehicle) * BLENDED_MPH) / 60,
      priorProvider: () => (this.globalRate.n >= 5 ? this.globalRate.mean : null),
    });
    this.engine = new VerdictEngine({
      tipEstimator: (o) => this.tips.expectedTip(o, ""),
      merchantWait: (o, now) => this.waits.waitEstimate(o.merchantId ?? "", now).p50,
      returnToDensity: (o) => {
        const rm = this.deadheadByArea.get(this.areaOf(o));
        return rm && rm.n >= 3 ? rm.mean : this.engine.config.defaultReturnToDensityMin;
      },
    });
  }

  private areaOf(offer: Offer): string {
    // Without a geocoder the dropoff town is the best available proxy for a
    // hex cluster; it is stable enough to learn a deadhead against.
    return (offer.dropoffAddress.split(",").pop() ?? "").trim().toLowerCase() || "unknown";
  }

  /** F6 context key: area × weather. Threshold estimation must not pool a
   *  dense corridor with a sparse one. */
  private contextOf(offer: Offer): string {
    return `${this.areaOf(offer)}|${this.weather}`;
  }

  get remainingMinutes(): number | null {
    if (!this.plannedEndAt) return null;
    return (this.plannedEndAt.getTime() - Date.now()) / 60000;
  }

  get onlineMinutes(): number {
    if (!this.startedAt) return 0;
    return (Date.now() - this.startedAt.getTime()) / 60000;
  }

  get acceptanceRate(): number {
    return this.offersSeen === 0 ? 0 : this.offersAccepted / this.offersSeen;
  }

  currentRate(now: Date, offer: Offer | null): RateDecision {
    return this.rates.currentRate(
      now, this.remainingMinutes, null,
      offer ? this.contextOf(offer) : `${this.currentArea}|${this.weather}`,
    );
  }

  /**
   * Score one offer. Records it either way — a decline is an observation about
   * this hour and place, and the reservation rate depends on having them.
   */
  async score(offer: Offer, confidence: number, now: Date): Promise<{
    verdict: Verdict; rate: RateDecision;
  }> {
    this.currentArea = this.areaOf(offer);
    this.tips.observeOffer(offer);
    const rate = this.currentRate(now, offer);
    const verdict = this.engine.evaluate(offer, now, rate, confidence, this.remainingMinutes);

    const tb = verdict.timeBreakdown ?? this.engine.buildTimeBreakdown(offer, now);
    const minutes = Math.max(totalMinutes(tb), 1e-9);
    const [payout] = this.engine.expectedPayout(offer);
    this.rates.observe(
      { seenAt: now, valueDollars: payout, durationMinutes: minutes },
      this.contextOf(offer),
    );
    this.globalRate.add(payout / minutes);
    this.offersSeen += 1;

    await putOffer({
      id: offer.offerId,
      seenAt: now.getTime(),
      displayedPayout: offer.displayedPayout,
      merchantName: offer.merchantName,
      merchantId: offer.merchantId,
      statedDistanceMi: offer.statedDistanceMi,
      statedMinutes: offer.statedMinutes,
      hitDisplayCap: offer.hitDisplayCap,
      projectedMinutes: minutes,
      projectedPayout: payout,
      reservationHourly: rate.hourly,
      verdict: verdict.color,
      extractionConfidence: confidence,
      action: null,
      actualPayout: null,
      actualMinutes: null,
    });
    return { verdict, rate };
  }

  /** The driver tapped accept. Mark the delivery window unavailable so lambda
   *  is measured per minute available (see reservation.ts). */
  async recordAccept(offer: Offer, projectedMinutes: number, now: Date): Promise<void> {
    this.offersAccepted += 1;
    this.rates.noteUnavailable(
      now, new Date(now.getTime() + projectedMinutes * 60000), this.contextOf(offer));
    await updateOffer(offer.offerId, { action: "accepted" });
  }

  async recordDecline(offerId: string): Promise<void> {
    await updateOffer(offerId, { action: "declined" });
  }

  /**
   * A completed delivery: the free label for F2 and the dwell sample for F4.
   * This is where the product's edge actually compounds.
   */
  async recordCompletion(
    offer: Offer,
    actualPayout: number,
    actualMinutes: number,
    merchantWaitMin: number | null,
    deadheadMin: number | null,
    now: Date,
  ): Promise<void> {
    this.tips.observeDelivery(offer, actualPayout, "");
    if (merchantWaitMin !== null && offer.merchantId) {
      this.waits.observeDwell(
        offer.merchantId, now, new Date(now.getTime() + merchantWaitMin * 60000));
    }
    if (deadheadMin !== null) {
      const area = this.areaOf(offer);
      let rm = this.deadheadByArea.get(area);
      if (!rm) {
        rm = new RunningMean();
        this.deadheadByArea.set(area, rm);
      }
      rm.add(deadheadMin);
    }
    this.mileage.grossEarnings += actualPayout;
    await updateOffer(offer.offerId, { actualPayout, actualMinutes });
  }

  addMiles(miles: number): void {
    if (miles <= 0 || !this.startedAt) return;
    // Re-logged as a single running entry so the deduction always reflects
    // every online mile, deadhead included.
    this.mileage.entries.length = 0;
    this.mileage.logSession({
      startTime: this.startedAt,
      endTime: new Date(Math.max(Date.now(), this.startedAt.getTime() + 60000)),
      startLoc: [0, 0], endLoc: [0, 0], miles, gross: 0,
    });
  }

  /** Replay the stored log so the models are warm on the next launch. */
  async hydrate(): Promise<number> {
    const stored: StoredOffer[] = await allOffers();
    stored.sort((a, b) => a.seenAt - b.seenAt);
    for (const s of stored) {
      this.tips.capDetector.observe(s.displayedPayout);
      if (s.projectedMinutes > 0) {
        this.globalRate.add(s.projectedPayout / s.projectedMinutes);
      }
    }
    return stored.length;
  }
}
