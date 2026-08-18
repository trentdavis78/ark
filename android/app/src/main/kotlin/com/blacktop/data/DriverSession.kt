package com.blacktop.data

import android.content.Context
import com.blacktop.domain.Offer
import com.blacktop.domain.OfferObservation
import com.blacktop.domain.MerchantWaitOracle
import com.blacktop.domain.RateDecision
import com.blacktop.domain.ReservationRateEngine
import com.blacktop.domain.TipEstimator
import com.blacktop.domain.Verdict
import com.blacktop.domain.VerdictEngine
import com.blacktop.hud.HudService

/**
 * Live session state: the one object that owns the learned models, so a
 * verdict, a completed delivery, and the running net-per-hour display all read
 * the same thing.
 *
 * Everything here trains on the driver's own work and stays on the device
 * (I5). Nothing syncs.
 */
class DriverSession(
    private val context: Context,
    private val log: OfferLog,
    vehicleCostPerMile: Double = 0.28,
) {
    val tips = TipEstimator()
    val waits = MerchantWaitOracle()

    private val globalRate = RunningMean()
    private val deadheadByArea = HashMap<String, RunningMean>()
    private var currentArea = ""

    val rates = ReservationRateEngine(
        // Never recommend an offer that fails to clear the cost of driving it.
        marginalCostPerMin = vehicleCostPerMile * BLENDED_MPH / 60.0,
        priorProvider = { if (globalRate.n >= 5) globalRate.mean else null },
    )

    private val engine = VerdictEngine(
        tipEstimator = { tips.expectedTip(it) },
        merchantWait = { offer, now -> waits.waitEstimate(offer.merchantId.orEmpty(), now).p50 },
        returnToDensity = { offer, _ ->
            deadheadByArea[areaOf(offer)]?.takeIf { it.n >= 3 }?.mean ?: DEFAULT_DEADHEAD_MIN
        },
    )

    var isOnline: Boolean = false
        private set
    private var startedAtMs: Long = 0L
    private var plannedEndMs: Long? = null
    var offersSeen: Int = 0
        private set
    var offersAccepted: Int = 0
        private set
    var grossEarnings: Double = 0.0
        private set

    fun goOnline(plannedHours: Double?) {
        isOnline = true
        startedAtMs = System.currentTimeMillis()
        plannedEndMs = plannedHours?.let { startedAtMs + (it * 3_600_000).toLong() }
    }

    fun goOffline() {
        isOnline = false
        HudService.hide(context)
    }

    val onlineMinutes: Double
        get() = if (!isOnline) 0.0 else (System.currentTimeMillis() - startedAtMs) / 60_000.0

    val acceptanceRate: Double
        get() = if (offersSeen == 0) 0.0 else offersAccepted.toDouble() / offersSeen

    private val remainingMinutes: Double?
        get() = plannedEndMs?.let { (it - System.currentTimeMillis()) / 60_000.0 }

    /** Without a geocoder the dropoff town is the best proxy for a hex cluster,
     *  and it is stable enough to learn a deadhead against. */
    private fun areaOf(offer: Offer): String =
        offer.dropoffAddress.substringAfterLast(',').trim().lowercase()
            .ifEmpty { offer.merchantName.substringAfterLast('—').trim().lowercase() }
            .ifEmpty { "unknown" }

    /** F6 context key. Threshold estimation must not pool a dense corridor
     *  with a sparse one. */
    private fun contextOf(offer: Offer): String = "${areaOf(offer)}|clear"

    fun currentRate(nowEpochMinutes: Long, offer: Offer?): RateDecision =
        rates.currentRate(
            nowEpochMinutes,
            remainingMinutes,
            null,
            offer?.let(::contextOf) ?: "$currentArea|clear",
        )

    /**
     * Score an offer and put it on the HUD. Records it either way — a decline
     * is an observation about this hour and place, and the reservation rate
     * depends on having them.
     */
    fun scoreAndShow(offer: Offer, confidence: Double): Verdict {
        val now = offer.seenAtEpochMinutes
        currentArea = areaOf(offer)
        tips.observeOffer(offer)

        val rate = currentRate(now, offer)
        val verdict = engine.evaluate(offer, now, rate, confidence, remainingMinutes)

        val tb = verdict.timeBreakdown ?: engine.buildTimeBreakdown(offer, now)
        val minutes = maxOf(tb.totalMinutes, 1e-9)
        val (payout, _) = engine.expectedPayout(offer)
        rates.observe(OfferObservation(now, payout, minutes), contextOf(offer))
        globalRate.add(payout / minutes)
        offersSeen += 1

        log.record(offer, verdict, minutes, payout)
        HudService.show(context, verdict)
        return verdict
    }

    /** The driver tapped accept in the delivery app. Marks the delivery window
     *  unavailable so lambda is measured per minute available. */
    fun recordAccept(offer: Offer, projectedMinutes: Double) {
        offersAccepted += 1
        val now = System.currentTimeMillis() / 60_000L
        rates.noteUnavailable(now, now + projectedMinutes.toLong(), contextOf(offer))
        log.markAction(offer.offerId, "accepted")
    }

    fun recordDecline(offerId: String) = log.markAction(offerId, "declined")

    /** A completed delivery: the free label for F2 and the dwell sample for F4.
     *  This is where the edge actually compounds. */
    fun recordCompletion(
        offer: Offer,
        actualPayout: Double,
        merchantWaitMin: Double?,
        deadheadMin: Double?,
    ) {
        tips.observeDelivery(offer, actualPayout)
        val merchantId = offer.merchantId
        if (merchantWaitMin != null && merchantId != null) {
            val arrived = System.currentTimeMillis() / 60_000L
            waits.observeDwell(merchantId, arrived, arrived + merchantWaitMin.toLong())
        }
        if (deadheadMin != null) {
            deadheadByArea.getOrPut(areaOf(offer)) { RunningMean() }.add(deadheadMin)
        }
        grossEarnings += actualPayout
        log.markCompleted(offer.offerId, actualPayout)
    }

    /** Replay the stored log so the models are warm on the next launch. */
    fun hydrate() {
        for (row in log.all()) {
            tips.capDetector.observe(row.displayedPayout)
            if (row.projectedMinutes > 0) globalRate.add(row.projectedPayout / row.projectedMinutes)
        }
    }

    private companion object {
        const val BLENDED_MPH = 20.0
        const val DEFAULT_DEADHEAD_MIN = 4.0
    }
}

class RunningMean {
    var n: Int = 0
        private set
    var mean: Double = 0.0
        private set

    fun add(x: Double) {
        n += 1
        mean += (x - mean) / n
    }
}
