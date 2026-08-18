package com.blacktop.data

import com.blacktop.domain.ParsedOffer
import com.blacktop.domain.Platform

/**
 * What the reader actually saw.
 *
 * The regexes in `OfferParser` were written against the PRD's description of a
 * DoorDash offer card, not against a real accessibility tree. Whether the
 * payout arrives in `text` or `contentDescription`, whether "4.2 mi · 23 min"
 * is one node or three, whether the card is drawn to a canvas with no text
 * nodes at all — none of that is knowable without a device.
 *
 * So the first shift is an experiment, and this is its instrument. It keeps
 * the raw captured text alongside what the parser made of it, which turns
 * "the reader doesn't work" into a specific, fixable diff.
 *
 * Off by default. It holds screen text in memory, so it is opt-in, capped,
 * and never written anywhere the driver did not ask for.
 */
class CaptureDiagnostics {

    data class Entry(
        val atMs: Long,
        val platform: Platform,
        val rawText: String,
        val usable: Boolean,
        val confidence: Double,
        val foundFields: List<String>,
        val missingFields: List<String>,
        val payout: Double?,
        val distanceMi: Double?,
        val minutes: Double?,
        val merchant: String?,
    )

    /** Opt-in, and independent of whether the driver is online — capture has
     *  to be testable while parked, before a shift starts. */
    var enabled: Boolean = false

    private val entries = ArrayDeque<Entry>()

    fun record(platform: Platform, rawText: String, parsed: ParsedOffer) {
        if (!enabled) return
        val offer = parsed.offer
        val entry = Entry(
            atMs = System.currentTimeMillis(),
            platform = platform,
            rawText = rawText.take(MAX_TEXT_CHARS),
            usable = parsed.usable,
            confidence = parsed.confidence,
            foundFields = parsed.fieldConfidence.filterValues { it > 0.0 }.keys.sorted(),
            missingFields = parsed.fieldConfidence.filterValues { it == 0.0 }.keys.sorted(),
            payout = offer?.displayedPayout,
            distanceMi = offer?.statedDistanceMi,
            minutes = offer?.statedMinutes,
            merchant = offer?.merchantName?.ifEmpty { null },
        )
        synchronized(entries) {
            entries.addLast(entry)
            while (entries.size > MAX_ENTRIES) entries.removeFirst()
        }
    }

    fun all(): List<Entry> = synchronized(entries) { entries.toList().asReversed() }

    fun clear() = synchronized(entries) { entries.clear() }

    val size: Int get() = synchronized(entries) { entries.size }

    /**
     * A report to paste to whoever is fixing the parser.
     *
     * The raw text is included because it is the entire point — without it the
     * report says "parsing failed" and nothing actionable. It is also screen
     * content, so the UI warns before sharing and the driver reads it first.
     */
    fun report(): String = buildString {
        appendLine("BLACKTOP capture diagnostics")
        appendLine("entries: ${size}")
        appendLine()
        for ((i, e) in all().withIndex()) {
            appendLine("--- capture ${i + 1} — ${e.platform} ---")
            appendLine("parsed: ${if (e.usable) "OK" else "FAILED"}  " +
                       "confidence=${"%.2f".format(e.confidence)}")
            appendLine("found:   ${e.foundFields.joinToString(", ").ifEmpty { "nothing" }}")
            appendLine("missing: ${e.missingFields.joinToString(", ").ifEmpty { "nothing" }}")
            appendLine("payout=${e.payout} distance=${e.distanceMi} " +
                       "minutes=${e.minutes} merchant=${e.merchant}")
            appendLine("raw text as the accessibility tree gave it:")
            appendLine(e.rawText.lines().joinToString("\n") { "  | $it" })
            appendLine()
        }
    }

    private companion object {
        const val MAX_ENTRIES = 25
        const val MAX_TEXT_CHARS = 4_000
    }
}
