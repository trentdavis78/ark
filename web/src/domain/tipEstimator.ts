/**
 * F2 — Hidden-Tip Estimator.
 *
 * The label arrives free: after completion the driver sees the actual payout,
 * and the difference against what the card displayed *is* the concealed tip.
 * Every finished delivery is a training example that costs nothing to collect.
 *
 * This is the hierarchical partial-pooling estimator that makes the model
 * useful at delivery 20 rather than delivery 500: a merchant with three
 * observations is mostly its category, a category with three is mostly the
 * market.
 */

import type { Offer } from "./models";

const DEFAULT_MARKET_PRIOR_TIP = 3.5;
const DEFAULT_SHRINKAGE_K = 8;

export class RunningMean {
  n = 0;
  mean = 0;
  add(x: number): void {
    this.n += 1;
    this.mean += (x - this.mean) / this.n;
  }
}

/**
 * Detects the market's displayed-payout ceiling by counting.
 *
 * DoorDash appears to cap the displayed figure per market and append "total
 * may be higher". If offers keep landing on the identical dollar amount, that
 * amount is the ceiling — and "at cap" becomes a strong positive signal about
 * the tip hiding above it. Learning this needs no privileged access at all,
 * which is exactly why it cannot be taken away (I2).
 */
export class DisplayCapDetector {
  private counts = new Map<number, number>();

  constructor(private minRepeats = 5) {}

  observe(displayedPayout: number): void {
    if (!(displayedPayout > 0)) return;
    const key = Math.round(displayedPayout * 100);
    this.counts.set(key, (this.counts.get(key) ?? 0) + 1);
  }

  /**
   * The modal repeated value, once it has repeated enough to be a ceiling
   * rather than a coincidence. Ties resolve to the larger amount: a cap is an
   * upper bound, so the higher candidate is the safer reading.
   */
  get capValue(): number | null {
    let bestKey: number | null = null;
    let bestCount = 0;
    for (const [k, c] of this.counts) {
      if (c < this.minRepeats) continue;
      if (c > bestCount || (c === bestCount && bestKey !== null && k > bestKey)) {
        bestKey = k;
        bestCount = c;
      }
    }
    return bestKey === null ? null : bestKey / 100;
  }

  isAtCap(displayedPayout: number): boolean {
    const cap = this.capValue;
    return cap !== null && Math.abs(displayedPayout - cap) < 0.005;
  }
}

export class TipEstimator {
  readonly capDetector: DisplayCapDetector;
  private market = new RunningMean();
  private byCategory = new Map<string, RunningMean>();
  private byMerchant = new Map<string, RunningMean>();
  private capExcess = new RunningMean();

  constructor(
    private marketPriorTip = DEFAULT_MARKET_PRIOR_TIP,
    private shrinkageK = DEFAULT_SHRINKAGE_K,
    capDetector?: DisplayCapDetector,
  ) {
    this.capDetector = capDetector ?? new DisplayCapDetector();
  }

  /** Every offer seen — accepted or declined — feeds the cap detector. */
  observeOffer(offer: Offer): void {
    this.capDetector.observe(offer.displayedPayout);
  }

  /** A completed delivery: the free label. */
  observeDelivery(offer: Offer, actualPayout: number, merchantCategory = ""): void {
    const tipDelta = Math.max(0, actualPayout - offer.displayedPayout);
    if (offer.hitDisplayCap || this.capDetector.isAtCap(offer.displayedPayout)) {
      // Capped observations would drag the plain tip mean upward, so the
      // excess above the cap is learned separately and the base distribution
      // stays clean.
      this.capExcess.add(tipDelta);
      return;
    }
    this.market.add(tipDelta);
    if (merchantCategory) {
      this.bucket(this.byCategory, merchantCategory).add(tipDelta);
    }
    const key = offer.merchantId ?? offer.merchantName;
    if (key) this.bucket(this.byMerchant, key).add(tipDelta);
  }

  private bucket(map: Map<string, RunningMean>, key: string): RunningMean {
    let m = map.get(key);
    if (!m) {
      m = new RunningMean();
      map.set(key, m);
    }
    return m;
  }

  private pooled(stats: RunningMean | undefined, parent: number): number {
    if (!stats || stats.n === 0) return parent;
    return (stats.n * stats.mean + this.shrinkageK * parent) / (stats.n + this.shrinkageK);
  }

  expectedTip(offer: Offer, merchantCategory = ""): number {
    const marketEst = this.pooled(this.market, this.marketPriorTip);
    const catEst = merchantCategory
      ? this.pooled(this.byCategory.get(merchantCategory), marketEst)
      : marketEst;
    const key = offer.merchantId ?? offer.merchantName;
    const base = key ? this.pooled(this.byMerchant.get(key), catEst) : catEst;

    const atCap = offer.hitDisplayCap || this.capDetector.isAtCap(offer.displayedPayout);
    if (!atCap) return base;
    // At the ceiling the true payout is known to exceed what is shown, so the
    // estimate must never fall below the uncapped one.
    return Math.max(base, this.pooled(this.capExcess, base * 1.5));
  }

  get labelCount(): number {
    return this.market.n + this.capExcess.n;
  }
}
