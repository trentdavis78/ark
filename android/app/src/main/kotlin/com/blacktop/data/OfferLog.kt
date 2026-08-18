package com.blacktop.data

import android.content.Context
import com.blacktop.domain.Offer
import com.blacktop.domain.Verdict
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The offer log — every card seen, declines included.
 *
 * A declined offer is an observation about that hour and place, not an
 * absence: it is what makes the reservation rate solvable and the
 * counterfactual replay (F13) possible without experimentation. PRD §8 makes
 * the same point about the `offers` table.
 *
 * Stored as newline-delimited JSON in app-private storage. Not a database
 * because nothing here needs one yet, and a file the driver can export is
 * easier to reason about than an opaque store.
 */
class OfferLog(context: Context) {

    private val file = File(context.filesDir, "offers.ndjson")

    data class Row(
        val id: String,
        val seenAt: Long,
        val displayedPayout: Double,
        val merchantName: String,
        val projectedMinutes: Double,
        val projectedPayout: Double,
        val reservationHourly: Double,
        val verdict: String,
        val confidence: Double,
        val source: String,
        val action: String?,
        val actualPayout: Double?,
    )

    fun record(offer: Offer, verdict: Verdict, minutes: Double, payout: Double) {
        val json = JSONObject()
            .put("id", offer.offerId)
            .put("seenAt", offer.seenAtEpochMinutes)
            .put("displayedPayout", offer.displayedPayout)
            .put("merchantName", offer.merchantName)
            .put("statedDistanceMi", offer.statedDistanceMi ?: JSONObject.NULL)
            .put("statedMinutes", offer.statedMinutes ?: JSONObject.NULL)
            .put("hitDisplayCap", offer.hitDisplayCap)
            .put("projectedMinutes", minutes)
            .put("projectedPayout", payout)
            .put("reservationHourly", verdict.reservationRateHourly)
            .put("verdict", verdict.color.name)
            .put("confidence", verdict.parseConfidence)
            .put("source", offer.source.name)
        runCatching { file.appendText(json.toString() + "\n") }
    }

    fun markAction(offerId: String, action: String) = patch(offerId, "action", action)

    fun markCompleted(offerId: String, actualPayout: Double) =
        patch(offerId, "actualPayout", actualPayout)

    /**
     * Append-only with a rewrite on patch. The log is a few thousand lines at
     * most over a season, so simplicity beats an index here.
     */
    private fun patch(offerId: String, key: String, value: Any) {
        if (!file.exists()) return
        runCatching {
            val updated = file.readLines().joinToString("\n") { line ->
                if (line.isBlank()) return@joinToString line
                val obj = JSONObject(line)
                if (obj.optString("id") == offerId) obj.put(key, value).toString() else line
            }
            file.writeText(updated + "\n")
        }
    }

    fun all(): List<Row> {
        if (!file.exists()) return emptyList()
        return runCatching {
            file.readLines().filter { it.isNotBlank() }.map { JSONObject(it) }.map { o ->
                Row(
                    id = o.optString("id"),
                    seenAt = o.optLong("seenAt"),
                    displayedPayout = o.optDouble("displayedPayout", 0.0),
                    merchantName = o.optString("merchantName"),
                    projectedMinutes = o.optDouble("projectedMinutes", 0.0),
                    projectedPayout = o.optDouble("projectedPayout", 0.0),
                    reservationHourly = o.optDouble("reservationHourly", 0.0),
                    verdict = o.optString("verdict"),
                    confidence = o.optDouble("confidence", 0.0),
                    source = o.optString("source"),
                    action = if (o.isNull("action")) null else o.optString("action"),
                    actualPayout = if (o.isNull("actualPayout")) null
                                   else o.optDouble("actualPayout"),
                )
            }
        }.getOrDefault(emptyList())
    }

    /** The whole log, for the driver to take with them. It is their record. */
    fun exportJson(): String =
        JSONArray(all().map { row ->
            JSONObject()
                .put("id", row.id).put("seenAt", row.seenAt)
                .put("displayedPayout", row.displayedPayout)
                .put("merchantName", row.merchantName)
                .put("verdict", row.verdict).put("action", row.action ?: JSONObject.NULL)
                .put("actualPayout", row.actualPayout ?: JSONObject.NULL)
        }).toString(2)

    fun count(): Int = all().size
}
