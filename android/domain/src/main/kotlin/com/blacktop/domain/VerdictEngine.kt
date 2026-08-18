package com.blacktop.domain

/**
 * F1 — Offer Verdict Engine. The product; everything else supports it.
 *
 * Captured offer → five-term time model → value model (F2) → compare against
 * the dynamic reservation rate (F6) → [Verdict].
 *
 * Every external signal is injected, so the engine is deterministic, testable
 * without a device, and offline-first — the verdict must not wait on a network
 * round trip in a Hunterdon County dead zone. Where a provider is absent the
 * fallback is a documented heuristic, never a silent zero.
 */

/** Below this we refuse to emit a verdict and hand the card back (I6). */
const val MIN_PARSE_CONFIDENCE = 0.75

/** Within ±8% of the threshold the honest answer is "judgment call". */
const val AMBER_BAND = 0.08

private const val STATED_MINUTES_TO_MERCHANT_SHARE = 0.40

data class VerdictConfig(
    val minParseConfidence: Double = MIN_PARSE_CONFIDENCE,
    val amberBand: Double = AMBER_BAND,
    val avgSpeedMph: Double = 25.0,
    /** Platforms understate drive time; inflate what the card claims. */
    val trafficInflation: Double = 1.10,
    val defaultMerchantWaitMin: Double = 5.0,
    val defaultDropoffFrictionMin: Double = 2.0,
    val defaultReturnToDensityMin: Double = 4.0,
)

/** Cached routing. Returning null means "no estimate", not "zero minutes". */
interface RouteProvider {
    fun driveToMerchantMinutes(offer: Offer): Double?
    fun driveToCustomerMinutes(offer: Offer): Double?
}

class VerdictEngine(
    private val tipEstimator: ((Offer) -> Double)? = null,
    private val merchantWait: ((Offer, Long) -> Double)? = null,
    private val dropoffFriction: ((Offer) -> Double)? = null,
    private val returnToDensity: ((Offer, Long) -> Double)? = null,
    private val routeProvider: RouteProvider? = null,
    val config: VerdictConfig = VerdictConfig(),
) {

    fun buildTimeBreakdown(offer: Offer, now: Long): TimeBreakdown {
        var toMerchant = routeProvider?.driveToMerchantMinutes(offer)
        var toCustomer = routeProvider?.driveToCustomerMinutes(offer)
        if (toMerchant == null || toCustomer == null) {
            val (estM, estC) = heuristicDriveMinutes(offer)
            toMerchant = toMerchant ?: estM
            toCustomer = toCustomer ?: estC
        }
        return TimeBreakdown(
            driveToMerchantMin = toMerchant,
            merchantWaitMin = merchantWait?.invoke(offer, now) ?: config.defaultMerchantWaitMin,
            driveToCustomerMin = toCustomer,
            dropoffFrictionMin = dropoffFriction?.invoke(offer)
                ?: config.defaultDropoffFrictionMin,
            returnToDensityMin = returnToDensity?.invoke(offer, now)
                ?: config.defaultReturnToDensityMin,
        )
    }

    /** Fallback when routing is unavailable: split the card's own (inflated)
     *  estimate, or derive one from stated distance at a blended speed. */
    private fun heuristicDriveMinutes(offer: Offer): Pair<Double, Double> {
        val stated = offer.statedMinutes
        val distance = offer.statedDistanceMi
        val total = when {
            stated != null && stated > 0 -> stated * config.trafficInflation
            distance != null && distance > 0 ->
                distance / config.avgSpeedMph * 60.0 * config.trafficInflation
            else -> 15.0   // nothing to go on; deliberately conservative
        }
        return (total * STATED_MINUTES_TO_MERCHANT_SHARE) to
                (total * (1.0 - STATED_MINUTES_TO_MERCHANT_SHARE))
    }

    /** Returns expected total payout and the hidden-tip component of it. */
    fun expectedPayout(offer: Offer): Pair<Double, Double> {
        val tip = (tipEstimator?.invoke(offer) ?: 0.0).coerceAtLeast(0.0)
        return (offer.displayedPayout + tip) to tip
    }

    fun evaluate(
        offer: Offer,
        now: Long,
        reservation: RateDecision,
        parseConfidence: Double = 1.0,
        remainingSessionMinutes: Double? = null,
    ): Verdict {
        // A card we could not read is the driver's call, not ours. Emitting a
        // confident-looking number from a bad parse is the one failure mode
        // that would actively cost them money (PRD §11).
        if (parseConfidence < config.minParseConfidence) {
            return Verdict(
                color = VerdictColor.MANUAL_FALLBACK,
                projectedNetHourly = 0.0,
                reservationRateHourly = reservation.hourly,
                expectedPayout = offer.displayedPayout,
                expectedHiddenTip = 0.0,
                timeBreakdown = null,
                parseConfidence = parseConfidence,
                ttsText = "Couldn't read that one. Your call.",
            )
        }

        val tb = buildTimeBreakdown(offer, now)
        val (expected, tip) = expectedPayout(offer)
        val totalMin = maxOf(tb.totalMinutes, 1e-9)
        val hourly = expected / totalMin * 60.0
        val threshold = reservation.hourly

        val color: VerdictColor
        val tts: String
        when {
            completionRisk(totalMin, remainingSessionMinutes) -> {
                color = VerdictColor.RED
                tts = "Skip it. Won't finish before you're done."
            }
            hourly >= threshold * (1.0 + config.amberBand) -> {
                color = VerdictColor.GREEN
                tts = "Take it. ${Math.round(hourly)}."
            }
            hourly <= threshold * (1.0 - config.amberBand) -> {
                color = VerdictColor.RED
                tts = "Skip it. ${Math.round(hourly)} against ${Math.round(threshold)}."
            }
            else -> {
                color = VerdictColor.AMBER
                tts = "Close call. ${Math.round(hourly)} against ${Math.round(threshold)}."
            }
        }

        return Verdict(
            color = color,
            projectedNetHourly = hourly,
            reservationRateHourly = threshold,
            expectedPayout = expected,
            expectedHiddenTip = tip,
            timeBreakdown = tb,
            parseConfidence = parseConfidence,
            ttsText = tts,
        )
    }
}
