package com.blacktop.domain

/**
 * F1 extraction — offer-card text to a structured [Offer], with a confidence
 * score attached.
 *
 * This is the fast path. Text arrives from accessibility node traversal, so
 * there is no image, no OCR, and no network: a verdict inside the latency
 * budget. When it fails, `:app` falls back to a screenshot read by the vision
 * model, which is slower but survives a layout change.
 *
 * The card layout *will* change; PRD §11 treats that as a certainty rather
 * than a risk to avoid. So parsing reports how much it actually found and the
 * engine refuses to rule below [MIN_PARSE_CONFIDENCE]. Parse failures are
 * telemetry, not silent errors.
 *
 * Operates purely on text already rendered on the driver's own screen — no
 * platform request, no credential, no server response (I1/I2).
 */

data class ParsedOffer(
    val offer: Offer?,
    val confidence: Double,
    /** Per-field confidence, for telemetry on which part of the card moved. */
    val fieldConfidence: Map<String, Double>,
    val failures: List<String>,
) {
    val usable: Boolean get() = offer != null && confidence >= MIN_PARSE_CONFIDENCE
}

private val RE_PAYOUT = Regex("""\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)(\+)?""")
private val RE_DISTANCE = Regex("""(\d+(?:\.\d+)?)\s*(?:mi|miles)\b""", RegexOption.IGNORE_CASE)
private val RE_MINUTES = Regex("""(\d+)\s*(?:min|mins|minutes)\b""", RegexOption.IGNORE_CASE)
private val RE_PEAK = Regex("""peak\s*pay|\+\s?\$\d""", RegexOption.IGNORE_CASE)
private val RE_MAY_BE_HIGHER = Regex("""total\s+may\s+be\s+higher|may\s+be\s+higher""",
                                     RegexOption.IGNORE_CASE)
private val RE_GUARANTEE = Regex("""guaranteed|includes?\s+tip""", RegexOption.IGNORE_CASE)
private val RE_SHOP = Regex("""shop\s*(&|and)\s*deliver|shopping""", RegexOption.IGNORE_CASE)
private val RE_LARGE = Regex("""large\s+order""", RegexOption.IGNORE_CASE)
private val RE_STACKED = Regex("""stacked|2\s+orders|double""", RegexOption.IGNORE_CASE)
private val RE_DELIVER_BY = Regex("""deliver\s+by""", RegexOption.IGNORE_CASE)

/**
 * The payout is the first prominent dollar figure that is not a Peak Pay line
 * item — cards lead with it.
 */
private fun extractPayout(text: String): Pair<Double?, Boolean> {
    var mayBeHigher = RE_MAY_BE_HIGHER.containsMatchIn(text)
    val peakSpans = RE_PEAK.findAll(text).map { it.range }.toList()
    for (m in RE_PAYOUT.findAll(text)) {
        if (peakSpans.any { m.range.first in it }) continue
        val value = m.groupValues[1].replace(",", "").toDoubleOrNull() ?: continue
        if (m.groupValues[2].isNotEmpty()) mayBeHigher = true   // "$9.75+"
        return value to mayBeHigher
    }
    return null to mayBeHigher
}

/** The merchant name is the first line that is not money/distance/time chrome. */
private fun extractMerchant(lines: List<String>): String? = lines.firstOrNull { ln ->
    ln.length >= 2 &&
        !RE_PAYOUT.containsMatchIn(ln) && !RE_DISTANCE.containsMatchIn(ln) &&
        !RE_MINUTES.containsMatchIn(ln) && !RE_DELIVER_BY.containsMatchIn(ln) &&
        !RE_MAY_BE_HIGHER.containsMatchIn(ln) && !RE_GUARANTEE.containsMatchIn(ln) &&
        !RE_SHOP.containsMatchIn(ln) && !RE_LARGE.containsMatchIn(ln) &&
        !RE_STACKED.containsMatchIn(ln)
}

private fun offerTypeOf(text: String): OfferType = when {
    RE_SHOP.containsMatchIn(text) -> OfferType.SHOP_DELIVER
    RE_LARGE.containsMatchIn(text) -> OfferType.LARGE_ORDER
    RE_STACKED.containsMatchIn(text) -> OfferType.STACKED
    else -> OfferType.SINGLE
}

fun parseOfferText(
    text: String,
    offerId: String,
    nowEpochMinutes: Long,
    platform: Platform = Platform.DOORDASH,
    knownCapValue: Double? = null,
    source: CaptureSource = CaptureSource.ACCESSIBILITY_NODES,
): ParsedOffer {
    val lines = text.lines().map { it.trim() }.filter { it.isNotEmpty() }
    val joined = lines.joinToString("\n")
    val failures = mutableListOf<String>()
    val fc = mutableMapOf<String, Double>()

    val (payout, mayBeHigher) = extractPayout(joined)
    fc["payout"] = if (payout == null) 0.0 else 1.0
    if (payout == null) failures += "payout_not_found"

    val distance = RE_DISTANCE.find(joined)?.groupValues?.get(1)?.toDoubleOrNull()
    fc["distance"] = if (distance == null) 0.0 else 1.0
    if (distance == null) failures += "distance_not_found"

    val minutes = RE_MINUTES.find(joined)?.groupValues?.get(1)?.toDoubleOrNull()
    fc["minutes"] = if (minutes == null) 0.0 else 1.0
    if (minutes == null) failures += "minutes_not_found"

    val merchant = extractMerchant(lines)
    fc["merchant"] = if (merchant == null) 0.0 else 1.0
    if (merchant == null) failures += "merchant_not_found"

    // Payout carries half the weight: without it there is nothing to decide,
    // while a missing merchant name only costs some model resolution.
    val confidence = 0.50 * fc.getValue("payout") +
            0.20 * fc.getValue("distance") +
            0.20 * fc.getValue("minutes") +
            0.10 * fc.getValue("merchant")

    if (payout == null) {
        return ParsedOffer(null, confidence, fc, failures)
    }

    val atCap = mayBeHigher ||
        (knownCapValue != null && kotlin.math.abs(payout - knownCapValue) < 0.005)

    return ParsedOffer(
        offer = Offer(
            offerId = offerId,
            platform = platform,
            seenAtEpochMinutes = nowEpochMinutes,
            displayedPayout = payout,
            merchantName = merchant ?: "",
            merchantId = merchant,
            statedDistanceMi = distance,
            statedMinutes = minutes,
            offerType = offerTypeOf(joined),
            hitDisplayCap = atCap,
            source = source,
        ),
        confidence = confidence,
        fieldConfidence = fc,
        failures = failures,
    )
}

/** I6 — manual entry, which ships in v1 and works standalone. */
fun manualOffer(
    displayedPayout: Double,
    statedDistanceMi: Double?,
    statedMinutes: Double?,
    offerId: String,
    nowEpochMinutes: Long,
    merchantName: String = "",
): ParsedOffer = ParsedOffer(
    offer = Offer(
        offerId = offerId,
        platform = Platform.MANUAL,
        seenAtEpochMinutes = nowEpochMinutes,
        displayedPayout = displayedPayout,
        merchantName = merchantName,
        merchantId = merchantName.ifEmpty { null },
        statedDistanceMi = statedDistanceMi,
        statedMinutes = statedMinutes,
        source = CaptureSource.MANUAL,
    ),
    // Typed by a human looking straight at the card.
    confidence = 1.0,
    fieldConfidence = mapOf("payout" to 1.0, "distance" to 1.0,
                            "minutes" to 1.0, "merchant" to 1.0),
    failures = emptyList(),
)
