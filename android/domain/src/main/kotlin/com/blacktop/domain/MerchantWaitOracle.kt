package com.blacktop.domain

/**
 * F4 — Merchant Wait Oracle.
 *
 * Per *location*, never per brand: the same chain four miles apart behaves
 * completely differently, and averaging them destroys the only signal worth
 * having. Measured passively from geofence dwell — arrival at the merchant
 * polygon to departure — so it costs the driver no input at all.
 */

private const val DEFAULT_WAIT_MIN = 5.0
private const val MAX_SAMPLES_PER_BUCKET = 40
/** A driver who parked and took a break is not a three-hour pickup wait. */
private const val MAX_PLAUSIBLE_DWELL_MIN = 60.0

data class WaitEstimate(
    val p50: Double,
    val p90: Double,
    val sampleN: Int,
    val source: String,
)

private fun quantile(sorted: List<Double>, q: Double): Double {
    if (sorted.isEmpty()) return 0.0
    val idx = (sorted.size - 1) * q
    val lo = Math.floor(idx).toInt()
    val hi = Math.ceil(idx).toInt()
    val a = sorted[lo]
    val b = sorted[hi]
    return if (lo == hi) a else a + (b - a) * (idx - lo)
}

class MerchantWaitOracle(
    private val defaultWaitMin: Double = DEFAULT_WAIT_MIN,
    private val minHourSamples: Int = 4,
    private val minLocationSamples: Int = 3,
) {
    private val byHour = HashMap<String, ArrayDeque<Double>>()
    private val byLocation = HashMap<String, ArrayDeque<Double>>()

    private fun push(map: HashMap<String, ArrayDeque<Double>>, key: String,
                     value: Double, cap: Int) {
        val q = map.getOrPut(key) { ArrayDeque() }
        q.addLast(value)
        while (q.size > cap) q.removeFirst()
    }

    fun observeDwell(merchantId: String, arrivedAtEpochMinutes: Long,
                     departedAtEpochMinutes: Long) {
        val minutes = (departedAtEpochMinutes - arrivedAtEpochMinutes).toDouble()
        require(minutes >= 0) { "departure before arrival" }
        val clipped = minOf(minutes, MAX_PLAUSIBLE_DWELL_MIN)
        push(byHour, "$merchantId|${hourOfWeek(arrivedAtEpochMinutes)}", clipped,
             MAX_SAMPLES_PER_BUCKET)
        push(byLocation, merchantId, clipped, MAX_SAMPLES_PER_BUCKET * 4)
    }

    fun waitEstimate(merchantId: String, whenEpochMinutes: Long): WaitEstimate {
        val hourly = byHour["$merchantId|${hourOfWeek(whenEpochMinutes)}"]?.toList().orEmpty()
        if (hourly.size >= minHourSamples) {
            val xs = hourly.sorted()
            return WaitEstimate(quantile(xs, 0.5), quantile(xs, 0.9), xs.size, "hour")
        }
        val loc = byLocation[merchantId]?.toList().orEmpty()
        if (loc.size >= minLocationSamples) {
            val xs = loc.sorted()
            return WaitEstimate(quantile(xs, 0.5), quantile(xs, 0.9), xs.size, "location")
        }
        return WaitEstimate(defaultWaitMin, defaultWaitMin * 2, 0, "default")
    }

    fun chronicOffender(merchantId: String, whenEpochMinutes: Long,
                        p50Threshold: Double = 10.0): Boolean {
        val est = waitEstimate(merchantId, whenEpochMinutes)
        return est.sampleN >= minLocationSamples && est.p50 >= p50Threshold
    }

    fun flagText(merchantId: String, whenEpochMinutes: Long): String? {
        if (!chronicOffender(merchantId, whenEpochMinutes)) return null
        val est = waitEstimate(merchantId, whenEpochMinutes)
        return "This location runs ${Math.round(est.p50)} min at this hour, " +
               "P90 ${Math.round(est.p90)}."
    }

    /**
     * Minutes to delay departure toward the merchant. The food will not be
     * ready any sooner, and standing at a counter is pure unpaid loss — but
     * the delay is bounded so it can never create a lateness violation.
     */
    fun arrivalDelayAdvice(
        merchantId: String,
        whenEpochMinutes: Long,
        driveMinutes: Double,
        pickupDeadlineMinutes: Double? = null,
        counterBufferMin: Double = 2.0,
    ): Double {
        val est = waitEstimate(merchantId, whenEpochMinutes)
        var delay = (est.p50 - counterBufferMin).coerceAtLeast(0.0)
        if (pickupDeadlineMinutes != null) {
            val latestSafe = pickupDeadlineMinutes - driveMinutes - counterBufferMin
            delay = minOf(delay, latestSafe.coerceAtLeast(0.0))
        }
        return delay
    }
}
