package com.blacktop.data

import android.util.Log
import com.blacktop.domain.Platform

/**
 * Parse-failure telemetry.
 *
 * PRD §7: parse failures are telemetry, not silent errors. When DoorDash
 * changes the offer card — and they will — the failure rate is the signal that
 * says so, and the app must degrade rather than emit wrong verdicts.
 *
 * Local only. Nothing is transmitted; the counter exists so the driver and a
 * maintainer can see a layout change happening rather than quietly losing
 * accuracy.
 */
class Telemetry {
    private val failuresByField = HashMap<String, Int>()
    var totalParseFailures: Int = 0
        private set
    var lastFailureAtMs: Long = 0L
        private set

    fun recordParseFailure(platform: Platform, failures: List<String>, textLength: Int) {
        totalParseFailures += 1
        lastFailureAtMs = System.currentTimeMillis()
        failures.forEach { failuresByField[it] = (failuresByField[it] ?: 0) + 1 }
        Log.w(TAG, "parse failure on $platform: $failures (text=$textLength chars)")
    }

    /** True when failures have piled up fast enough to mean the layout moved,
     *  rather than the odd unreadable card. */
    fun layoutLikelyChanged(threshold: Int = 5): Boolean = totalParseFailures >= threshold

    fun summary(): String =
        if (totalParseFailures == 0) "no parse failures"
        else "$totalParseFailures parse failures: " +
             failuresByField.entries.sortedByDescending { it.value }
                 .joinToString(", ") { "${it.key} x${it.value}" }

    private companion object { const val TAG = "BlacktopTelemetry" }
}
