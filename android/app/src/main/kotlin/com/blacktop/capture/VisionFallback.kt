package com.blacktop.capture

import android.graphics.Bitmap
import android.util.Base64
import com.blacktop.BuildConfig
import com.blacktop.domain.CaptureSource
import com.blacktop.domain.Offer
import com.blacktop.domain.OfferType
import com.blacktop.domain.Platform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * The resilience layer.
 *
 * PRD §11 treats a DoorDash layout change as a certainty, not a risk: node
 * traversal and regex parsing will break, and the only question is what
 * happens next. The answer is that a screenshot goes to a vision model, which
 * reads the card by looking at it — the one approach a layout change does not
 * defeat.
 *
 * This is the slow path on purpose. It costs a round trip measured in seconds,
 * so it never runs while node capture is working. It fits inside a 30–45
 * second offer timer, not inside the 400ms budget, and the UI says which path
 * produced a verdict so the driver knows what they are looking at.
 *
 * The image is a capture of the driver's own screen, sent to BLACKTOP's own
 * endpoint. No platform server, no credential (I1/I2).
 */
class VisionFallback(
    private val screenCapture: ScreenCapture,
    private val endpoint: String = BuildConfig.EXTRACT_ENDPOINT,
    private val onOffer: (Offer, Double) -> Unit = { _, _ -> },
    private val onUnavailable: (String) -> Unit = {},
) {
    /** Set by the app when the driver grants MediaProjection. Without it the
     *  fallback is simply absent, and node capture carries the whole load. */
    var enabled: Boolean = false

    private var lastAttemptMs = 0L

    /**
     * Try the vision path. No-ops unless configured and enabled, and rate
     * limits itself — a broken layout fires parse failures continuously, and
     * one screenshot per offer timer is the most that could possibly help.
     */
    fun attempt(nodeConfidence: Double) {
        val now = System.currentTimeMillis()
        if (!enabled || endpoint.isEmpty()) return
        if (now - lastAttemptMs < MIN_INTERVAL_MS) return
        lastAttemptMs = now
        screenCapture.requestFrame { bitmap ->
            if (bitmap == null) {
                onUnavailable("Couldn't capture the screen.")
            } else {
                screenCapture.scope.launch(Dispatchers.IO) {
                    read(bitmap, nodeConfidence)
                }
            }
        }
    }

    private suspend fun read(bitmap: Bitmap, nodeConfidence: Double) {
        val result = withContext(Dispatchers.IO) { post(bitmap) }
        if (result == null) {
            onUnavailable("Reader unreachable — your call on this one.")
            return
        }
        val offer = result.first
        // The vision read replaces the node read entirely; it does not average
        // with it. A confident vision extraction of a card the parser could not
        // see is the whole point of this path.
        if (offer == null) onUnavailable("Couldn't read that card.")
        else onOffer(offer, maxOf(result.second, nodeConfidence))
    }

    private fun post(bitmap: Bitmap): Pair<Offer?, Double>? {
        val png = ByteArrayOutputStream().also {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
        }.toByteArray()
        val body = JSONObject()
            .put("imageBase64", Base64.encodeToString(png, Base64.NO_WRAP))
            .put("mediaType", "image/png")
            .toString()

        val conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
        }
        return try {
            conn.outputStream.use { it.write(body.toByteArray()) }
            if (conn.responseCode !in 200..299) return null
            val text = conn.inputStream.bufferedReader().use { it.readText() }
            parseResponse(JSONObject(text))
        } catch (_: Exception) {
            null
        } finally {
            conn.disconnect()
        }
    }

    /**
     * Every field is optional on the wire because the model is instructed to
     * return null rather than invent a value it cannot see. A missing payout
     * therefore yields no offer at all — a fabricated one would produce a
     * confident wrong verdict, which is worse for the driver than none.
     */
    private fun parseResponse(json: JSONObject): Pair<Offer?, Double> {
        if (!json.optBoolean("ok", false)) return null to 0.0
        val o = json.optJSONObject("offer") ?: return null to 0.0
        if (!o.optBoolean("isOfferCard", false)) return null to 0.0

        val payout = o.optDoubleOrNull("displayedPayout") ?: return null to 0.0
        val confidence = o.optDouble("extractionConfidence", 0.0)
        val merchant = o.optStringOrNull("merchantName")

        return Offer(
            offerId = "v-${System.currentTimeMillis()}",
            platform = when (o.optString("platform")) {
                "uber_eats" -> Platform.UBER_EATS
                "grubhub" -> Platform.GRUBHUB
                else -> Platform.DOORDASH
            },
            seenAtEpochMinutes = System.currentTimeMillis() / 60_000L,
            displayedPayout = payout,
            merchantName = merchant.orEmpty(),
            merchantId = merchant,
            dropoffAddress = o.optStringOrNull("dropoffAddress").orEmpty(),
            statedDistanceMi = o.optDoubleOrNull("statedDistanceMi"),
            statedMinutes = o.optDoubleOrNull("statedMinutes"),
            offerType = when (o.optString("offerType")) {
                "stacked" -> OfferType.STACKED
                "shop_deliver" -> OfferType.SHOP_DELIVER
                "large_order" -> OfferType.LARGE_ORDER
                else -> OfferType.SINGLE
            },
            peakPay = o.optDoubleOrNull("peakPay") ?: 0.0,
            hitDisplayCap = o.optBoolean("totalMayBeHigher", false),
            source = CaptureSource.VISION,
        ) to confidence
    }

    private companion object {
        const val MIN_INTERVAL_MS = 20_000L
        const val CONNECT_TIMEOUT_MS = 5_000
        const val READ_TIMEOUT_MS = 20_000
    }
}

/** JSON null and absent both mean "the card did not show this". */
private fun JSONObject.optDoubleOrNull(key: String): Double? =
    if (isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

private fun JSONObject.optStringOrNull(key: String): String? =
    if (isNull(key)) null else optString(key).takeIf { it.isNotEmpty() }
