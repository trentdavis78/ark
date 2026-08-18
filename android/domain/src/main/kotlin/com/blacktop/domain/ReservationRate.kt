package com.blacktop.domain

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
 * the same: the engine silently degenerating into the fixed threshold this
 * module exists to replace. See `docs/SIMULATION.md`; the reference simulator
 * lives in `core/blacktop/sim`.
 *
 *   1. The sanity band is a diagnostic, never a clamp.
 *   2. The window survives starvation and pools toward a learned prior.
 *   3. Windows are keyed by context; one market must not set another's rate.
 *   4. Lambda counts minutes *available*, not minutes elapsed.
 */

/** PRD F6: where w* is expected to land. Leaving it signals an upstream bug. */
val SANITY_BAND_PER_MIN = 0.30..0.85

const val DEFAULT_WINDOW_MINUTES = 30.0
const val DEFAULT_DECAY_HORIZON_MINUTES = 45.0
const val MIN_AVAILABLE_MINUTES = 1.0

/** One observed offer — accepted or declined. Both are observations. */
data class OfferObservation(
    val seenAtEpochMinutes: Long,
    val valueDollars: Double,
    val durationMinutes: Double,
) {
    val ratePerMin: Double
        get() = if (durationMinutes <= 0.0) 0.0 else valueDollars / durationMinutes
}

/** Acceptance-rate targeting inputs (PRD F6 policy constraints). */
data class ArPolicyInputs(
    val currentAcceptanceRate: Double,
    val targetAcceptanceRate: Double,
    val daysUntilMonthEnd: Int,
    /** AR only matters where Priority Access actually changes offer flow. */
    val zoneIsSparse: Boolean,
)

/** The rate with its full derivation, so the HUD can show its work. */
data class RateDecision(
    val wStarPerMin: Double,
    val hourly: Double,
    val rawWPerMin: Double,
    val decayFactor: Double,
    val arAdjustmentPerMin: Double,
    val outOfBand: Boolean,
    val floorApplied: Boolean,
    val arrivalRatePerMin: Double,
    val sampleN: Int,
    val notes: List<String> = emptyList(),
)

/**
 * Rolling estimate of the offer distribution and arrival rate for one context.
 *
 * A working driver only *sees* offers while idle, so a strict 20–40 minute wall
 * window holds two or three observations at high utilization and the
 * optimal-stopping solve never has enough data to run. Falling back to a
 * constant in that state turns the engine back into a fixed threshold, so the
 * window is bounded by time *and* by sample count: it reaches further back when
 * the nominal window is thin, and reports the span it actually covered.
 */
class RollingOfferWindow(
    private val windowMinutes: Double = DEFAULT_WINDOW_MINUTES,
    private val minSamples: Int = 5,
    retainMinutes: Double = 240.0,
) {
    init {
        require(windowMinutes in 15.0..60.0) { "windowMinutes should be in [15, 60]" }
    }

    private val retain = maxOf(retainMinutes, windowMinutes)
    private val observations = ArrayDeque<OfferObservation>()
    private val unavailable = ArrayDeque<Pair<Long, Long>>()

    fun observe(obs: OfferObservation) {
        observations.addLast(obs)
        evict(obs.seenAtEpochMinutes)
    }

    /**
     * Mark a stretch spent on a delivery, when no offer could have arrived.
     * This is the driver's own session timeline, not platform data (I5).
     */
    fun noteUnavailable(startEpochMinutes: Long, endEpochMinutes: Long) {
        if (endEpochMinutes > startEpochMinutes) {
            unavailable.addLast(startEpochMinutes to endEpochMinutes)
        }
    }

    /** Drop past the retention horizon — never merely past the window, since
     *  the older history is what rescues a starved one. */
    private fun evict(now: Long) {
        val cutoff = now - retain.toLong()
        while (observations.isNotEmpty() && observations.first().seenAtEpochMinutes < cutoff) {
            observations.removeFirst()
        }
        while (unavailable.isNotEmpty() && unavailable.first().second < cutoff) {
            unavailable.removeFirst()
        }
    }

    fun snapshot(now: Long): List<OfferObservation> {
        evict(now)
        val cutoff = now - windowMinutes.toLong()
        val fresh = observations.filter { it.seenAtEpochMinutes >= cutoff }
        if (fresh.size >= minSamples) return fresh
        return observations.toList().takeLast(minSamples)
    }

    private fun spanStart(now: Long): Long {
        val obs = snapshot(now)
        val nominalStart = now - windowMinutes.toLong()
        if (obs.size < 2) return nominalStart
        return minOf(obs.first().seenAtEpochMinutes, nominalStart)
    }

    fun spanMinutes(now: Long): Double =
        maxOf((now - spanStart(now)).toDouble(), MIN_AVAILABLE_MINUTES)

    /** Elapsed minutes in the span minus time spent on deliveries. */
    fun availableMinutes(now: Long): Double {
        val start = spanStart(now)
        var busy = 0.0
        for ((b0, b1) in unavailable) {
            val lo = maxOf(b0, start)
            val hi = minOf(b1, now)
            if (hi > lo) busy += (hi - lo).toDouble()
        }
        return maxOf((now - start).toDouble() - busy, MIN_AVAILABLE_MINUTES)
    }

    /**
     * Offers seen per minute *available*.
     *
     * Dividing by elapsed time instead ignores that offers arriving
     * mid-delivery are never seen. That understates lambda, lengthens the
     * modelled wait for a better draw, depresses w*, and makes the engine
     * accept offers it should decline — worth roughly 15 points of excess
     * acceptance in every simulated zone before this correction.
     */
    fun arrivalRatePerMin(now: Long): Double {
        val obs = snapshot(now)
        if (obs.isEmpty()) return 0.0
        return obs.size / availableMinutes(now)
    }

    val size: Int get() = observations.size
}

/**
 * Solve the optimal-stopping fixed point on the empirical distribution.
 *
 * Scans a threshold at each observed rate and returns the earning rate at the
 * maximizing one, which equals w* at the indifference point. Returns 0.0 when
 * there is nothing to go on, and the caller falls back to its prior.
 */
fun solveWStar(observations: List<OfferObservation>, arrivalRatePerMin: Double): Double {
    val obs = observations.filter { it.durationMinutes > 0.0 }
    if (obs.isEmpty() || arrivalRatePerMin <= 0.0) return 0.0
    var best = 0.0
    for (w in obs.map { it.ratePerMin }.distinct().sorted()) {
        val accepted = obs.filter { it.ratePerMin >= w }
        if (accepted.isEmpty()) continue
        val p = accepted.size.toDouble() / obs.size
        val meanValue = accepted.sumOf { it.valueDollars } / accepted.size
        val meanDuration = accepted.sumOf { it.durationMinutes } / accepted.size
        val expectedWait = 1.0 / (arrivalRatePerMin * p)
        best = maxOf(best, meanValue / (expectedWait + meanDuration))
    }
    return best
}

/**
 * Inside the final stretch of a planned session the rate decays toward the
 * floor: there is no future left to wait for. Null (open-ended) means 1.0.
 */
fun sessionEndDecay(
    remainingMinutes: Double?,
    horizonMinutes: Double = DEFAULT_DECAY_HORIZON_MINUTES,
): Double {
    if (remainingMinutes == null) return 1.0
    if (remainingMinutes >= horizonMinutes) return 1.0
    return (remainingMinutes / horizonMinutes).coerceAtLeast(0.0)
}

/**
 * Temporarily lower w* to farm acceptance rate for Priority Access — but only
 * where it changes anything: sparse zones, near month end, AR below target.
 */
fun arFarmingAdjustment(wPerMin: Double, ar: ArPolicyInputs?): Pair<Double, String?> {
    if (ar == null || !ar.zoneIsSparse) return 0.0 to null
    val deficit = ar.targetAcceptanceRate - ar.currentAcceptanceRate
    if (deficit <= 0.0 || ar.daysUntilMonthEnd > 7) return 0.0 to null
    val scale = minOf(1.0, deficit / 0.10)
    val adj = -minOf(0.25 * wPerMin, 0.25 * wPerMin * scale)
    return adj to ("AR farming: %.0f%% vs target %.0f%%, %dd left in month"
        .format(ar.currentAcceptanceRate * 100, ar.targetAcceptanceRate * 100,
                ar.daysUntilMonthEnd))
}

/**
 * Guardrail: true when accepting likely cannot finish inside the planned
 * session. F1 downgrades any GREEN to RED when this fires, because completion
 * rate ≥ 95% is release-blocking (PRD §4).
 */
fun completionRisk(
    projectedMinutes: Double,
    remainingSessionMinutes: Double?,
    graceMinutes: Double = 20.0,
): Boolean {
    if (remainingSessionMinutes == null) return false
    return projectedMinutes > remainingSessionMinutes + graceMinutes
}

/**
 * Maintains the per-context windows and produces the layered decision.
 *
 * PRD F6 scopes the estimate to "the current hex cluster × hour × weather
 * bucket", and that scoping is not decorative: one shared window lets a dense
 * corridor's distribution set a sparse zone's threshold, and the sparse zone
 * then declines nearly everything it is offered. Hour is left to recency, since
 * a 20–40 minute window sits inside one hour by construction.
 */
class ReservationRateEngine(
    private val windowMinutes: Double = DEFAULT_WINDOW_MINUTES,
    private val priorWPerMin: Double = 0.50,
    private val minSamples: Int = 5,
    private val shrinkageK: Double = 4.0,
    /** Never recommend an offer that fails to clear the cost of driving it. */
    private val marginalCostPerMin: Double = 0.0,
    /** Context prior learned from the driver's own history. */
    private val priorProvider: ((Long) -> Double?)? = null,
) {
    private val windows = HashMap<String, RollingOfferWindow>()

    private fun windowFor(context: String): RollingOfferWindow =
        windows.getOrPut(context) {
            RollingOfferWindow(windowMinutes, minSamples = minSamples)
        }

    fun observe(obs: OfferObservation, context: String = "") =
        windowFor(context).observe(obs)

    fun noteUnavailable(startEpochMinutes: Long, endEpochMinutes: Long,
                        context: String = "") =
        windowFor(context).noteUnavailable(startEpochMinutes, endEpochMinutes)

    private fun priorFor(now: Long): Double {
        val p = priorProvider?.invoke(now)
        return if (p != null && p > 0.0) p else priorWPerMin
    }

    fun currentRate(
        now: Long,
        remainingSessionMinutes: Double? = null,
        ar: ArPolicyInputs? = null,
        context: String = "",
    ): RateDecision {
        val window = windowFor(context)
        val obs = window.snapshot(now)
        val lambda = window.arrivalRatePerMin(now)
        val notes = mutableListOf<String>()

        val prior = priorFor(now)
        val solved = if (obs.size >= 2) solveWStar(obs, lambda) else 0.0
        val n = obs.size
        val raw: Double
        if (solved <= 0.0) {
            raw = prior
            notes += "cold start: %d usable samples, using prior %.3f/min".format(n, prior)
        } else {
            // Partial pooling, the same shrinkage F2 uses. A hard cold-start
            // switch to a constant is what starved the rate in practice;
            // pooling degrades smoothly and washes out as data arrives.
            raw = (n * solved + shrinkageK * prior) / (n + shrinkageK)
            if (n < minSamples) {
                notes += "thin window: %d samples, pooled %.3f toward prior %.3f"
                    .format(n, solved, prior)
            }
        }

        val decay = sessionEndDecay(remainingSessionMinutes)
        if (decay < 1.0) notes += "session-end decay x%.2f".format(decay)
        var w = raw * decay

        val (arAdj, arNote) = arFarmingAdjustment(w, ar)
        if (arNote != null) notes += arNote
        w += arAdj

        val floorApplied = w < marginalCostPerMin
        if (floorApplied) {
            notes += "marginal-cost floor: %.3f -> %.3f/min".format(w, marginalCostPerMin)
            w = marginalCostPerMin
        }

        // Diagnostic only. Clamping to the band edge would silently reinstate a
        // fixed threshold, and in a lean market it declines every offer
        // available.
        val outOfBand = w !in SANITY_BAND_PER_MIN
        if (outOfBand) {
            notes += "outside sanity band: %.3f/min not in [%.2f, %.2f] — check upstream"
                .format(w, SANITY_BAND_PER_MIN.start, SANITY_BAND_PER_MIN.endInclusive)
        }

        return RateDecision(
            wStarPerMin = w,
            hourly = w * 60.0,
            rawWPerMin = raw,
            decayFactor = decay,
            arAdjustmentPerMin = arAdj,
            outOfBand = outOfBand,
            floorApplied = floorApplied,
            arrivalRatePerMin = lambda,
            sampleN = n,
            notes = notes,
        )
    }
}
