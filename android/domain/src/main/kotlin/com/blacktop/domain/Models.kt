package com.blacktop.domain

/**
 * Shared vocabulary for the BLACKTOP decision engine.
 *
 * This module is deliberately free of Android imports. Nothing here touches
 * the network, the platform, or the screen: it takes an offer that has already
 * been read off the driver's own display and returns an advisory value object.
 * Capture lives in `:app`, and so does everything that could conceivably act —
 * nothing in this package can tap anything (I3/I4).
 *
 * Money is dollars, time is minutes, distance is miles.
 */

enum class Platform { DOORDASH, UBER_EATS, GRUBHUB, MANUAL }

enum class OfferType { SINGLE, STACKED, SHOP_DELIVER, LARGE_ORDER }

/** One glance, one colour (PRD §9.1). */
enum class VerdictColor {
    GREEN,            // take it
    AMBER,            // inside the indifference band — genuinely a judgment call
    RED,              // skip it
    MANUAL_FALLBACK,  // confidence too low; no verdict is emitted at all
}

enum class WeatherBucket { CLEAR, RAIN, SNOW, SEVERE }

enum class DestinationClass {
    SINGLE_FAMILY, MULTI_UNIT, HIGH_RISE, COMMERCIAL, CAMPUS, HOTEL, UNKNOWN
}

/** Where an offer's fields came from. Drives both telemetry and the UI's
 *  honesty about how it read the card. */
enum class CaptureSource {
    /** Accessibility node traversal — no image leaves the device, sub-second. */
    ACCESSIBILITY_NODES,
    /** Screenshot read by the vision model when nodes were unusable. */
    VISION,
    /** Typed by the driver. */
    MANUAL,
}

/** 0..167, 0 = Monday 00:00 local. */
fun hourOfWeek(epochMinutes: Long, zoneOffsetMinutes: Int = 0): Int {
    val local = epochMinutes + zoneOffsetMinutes
    val minuteOfWeek = Math.floorMod(local - MONDAY_EPOCH_MINUTES, MINUTES_PER_WEEK)
    return (minuteOfWeek / 60).toInt()
}

// 1970-01-05 was a Monday; anchoring here keeps hour-of-week arithmetic
// independent of any calendar library.
private const val MONDAY_EPOCH_MINUTES = 4L * 24L * 60L
private const val MINUTES_PER_WEEK = 7L * 24L * 60L

/**
 * An offer card as observed on the driver's own screen.
 *
 * Fields the card did not show are null rather than zero. The difference
 * between "this delivery has no pickup wait" and "the card never mentions
 * pickup wait" is the entire premise of the product, and a default of 0.0
 * erases it.
 */
data class Offer(
    val offerId: String,
    val platform: Platform,
    val seenAtEpochMinutes: Long,
    val displayedPayout: Double,
    val merchantName: String = "",
    val merchantId: String? = null,
    val merchantAddress: String = "",
    val dropoffAddress: String = "",
    val dropoffHex: String? = null,
    val dropoffClass: DestinationClass = DestinationClass.UNKNOWN,
    val statedDistanceMi: Double? = null,
    val statedMinutes: Double? = null,
    val offerType: OfferType = OfferType.SINGLE,
    val peakPay: Double = 0.0,
    /** The strongest single feature in the tip model, and it costs only counting. */
    val hitDisplayCap: Boolean = false,
    val itemCount: Int? = null,
    val subtotal: Double? = null,
    val source: CaptureSource = CaptureSource.ACCESSIBILITY_NODES,
)

/** The five-term time model of F1. */
data class TimeBreakdown(
    val driveToMerchantMin: Double,
    val merchantWaitMin: Double,
    val driveToCustomerMin: Double,
    val dropoffFrictionMin: Double,
    /** The deadhead back to density. No platform surfaces it. */
    val returnToDensityMin: Double,
) {
    val totalMinutes: Double
        get() = driveToMerchantMin + merchantWaitMin + driveToCustomerMin +
                dropoffFrictionMin + returnToDensityMin
}

/**
 * The advisory output. It is rendered and spoken; nothing consumes it that can
 * act on the driver's behalf (I3/I4).
 */
data class Verdict(
    val color: VerdictColor,
    val projectedNetHourly: Double,
    val reservationRateHourly: Double,
    val expectedPayout: Double,
    val expectedHiddenTip: Double,
    val timeBreakdown: TimeBreakdown?,
    val parseConfidence: Double,
    val ttsText: String,
) {
    val isActionable: Boolean get() = color != VerdictColor.MANUAL_FALLBACK

    /**
     * PRD §9.5: never show a number without what it is compared against.
     * "$38/hr" is not a decision; "$38 vs $31" is.
     */
    fun headline(): String =
        "%.0f vs %.0f".format(projectedNetHourly, reservationRateHourly)
}
