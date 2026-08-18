package com.blacktop.domain

import kotlin.math.abs

/**
 * F2 — Hidden-Tip Estimator.
 *
 * The label arrives free: after completion the driver sees the actual payout,
 * and the difference against what the card displayed *is* the concealed tip.
 * Every finished delivery is a training example that costs nothing to collect.
 *
 * The production model is a gradient-boosted tree exported to ONNX. This is
 * the hierarchical partial-pooling estimator underneath it, which is what makes
 * the model useful at delivery 20 rather than delivery 500: a merchant with
 * three observations is mostly its category, a category with three is mostly
 * the market.
 */

private const val DEFAULT_MARKET_PRIOR_TIP = 3.50
private const val DEFAULT_SHRINKAGE_K = 8.0

internal class RunningMean {
    var n: Int = 0
        private set
    var mean: Double = 0.0
        private set

    fun add(x: Double) {
        n += 1
        mean += (x - mean) / n
    }
}

/**
 * Detects the market's displayed-payout ceiling by counting.
 *
 * DoorDash appears to cap the displayed figure per market and append "total
 * may be higher". If offers keep landing on the identical dollar amount, that
 * amount is the ceiling — and "at cap" then becomes a strong positive signal
 * about the tip hiding above it. Learning this needs no privileged access at
 * all, which is exactly why no platform can take it away (I2).
 */
class DisplayCapDetector(private val minRepeats: Int = 5) {
    private val counts = LinkedHashMap<Long, Int>()

    private fun key(payout: Double): Long = Math.round(payout * 100.0)

    fun observe(displayedPayout: Double) {
        if (displayedPayout <= 0.0) return
        val k = key(displayedPayout)
        counts[k] = (counts[k] ?: 0) + 1
    }

    /**
     * The modal repeated value, once it has repeated enough to be a ceiling
     * rather than a coincidence. Ties resolve to the larger amount: a cap is
     * an upper bound, so the higher candidate is the safer reading.
     */
    val capValue: Double?
        get() {
            var bestKey: Long? = null
            var bestCount = 0
            for ((k, c) in counts) {
                if (c < minRepeats) continue
                if (c > bestCount || (c == bestCount && bestKey != null && k > bestKey)) {
                    bestKey = k
                    bestCount = c
                }
            }
            return bestKey?.let { it / 100.0 }
        }

    fun isAtCap(displayedPayout: Double): Boolean {
        val cap = capValue ?: return false
        return abs(displayedPayout - cap) < 0.005
    }
}

class TipEstimator(
    private val marketPriorTip: Double = DEFAULT_MARKET_PRIOR_TIP,
    private val shrinkageK: Double = DEFAULT_SHRINKAGE_K,
    val capDetector: DisplayCapDetector = DisplayCapDetector(),
) {
    private val market = RunningMean()
    private val byCategory = HashMap<String, RunningMean>()
    private val byMerchant = HashMap<String, RunningMean>()
    private val capExcess = RunningMean()

    /** Every offer seen — accepted or declined — feeds the cap detector. */
    fun observeOffer(offer: Offer) = capDetector.observe(offer.displayedPayout)

    /** A completed delivery: the free label. */
    fun observeDelivery(offer: Offer, actualPayout: Double, merchantCategory: String = "") {
        val tipDelta = (actualPayout - offer.displayedPayout).coerceAtLeast(0.0)
        if (offer.hitDisplayCap || capDetector.isAtCap(offer.displayedPayout)) {
            // Capped observations would drag the plain tip mean upward, so the
            // excess above the cap is learned on its own and the base
            // distribution stays clean.
            capExcess.add(tipDelta)
            return
        }
        market.add(tipDelta)
        if (merchantCategory.isNotEmpty()) {
            byCategory.getOrPut(merchantCategory) { RunningMean() }.add(tipDelta)
        }
        val key = offer.merchantId ?: offer.merchantName
        if (key.isNotEmpty()) byMerchant.getOrPut(key) { RunningMean() }.add(tipDelta)
    }

    private fun pooled(stats: RunningMean?, parent: Double): Double {
        if (stats == null || stats.n == 0) return parent
        return (stats.n * stats.mean + shrinkageK * parent) / (stats.n + shrinkageK)
    }

    fun expectedTip(offer: Offer, merchantCategory: String = ""): Double {
        val marketEst = pooled(market, marketPriorTip)
        val catEst = if (merchantCategory.isEmpty()) marketEst
                     else pooled(byCategory[merchantCategory], marketEst)
        val key = offer.merchantId ?: offer.merchantName
        val base = if (key.isEmpty()) catEst else pooled(byMerchant[key], catEst)

        val atCap = offer.hitDisplayCap || capDetector.isAtCap(offer.displayedPayout)
        if (!atCap) return base
        // At the ceiling the true payout is known to exceed what is shown, so
        // the estimate must never fall below the uncapped one.
        return maxOf(base, pooled(capExcess, base * 1.5))
    }

    val labelCount: Int get() = market.n + capExcess.n
}
