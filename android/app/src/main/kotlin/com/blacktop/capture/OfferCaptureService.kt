package com.blacktop.capture

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.blacktop.BlacktopApp
import com.blacktop.domain.CaptureSource
import com.blacktop.domain.Platform
import com.blacktop.domain.parseOfferText

/**
 * F1 capture — the fast path.
 *
 * When a delivery app draws an offer card, its text is already in the
 * accessibility tree. Reading it there costs no screenshot, no OCR, and no
 * network, which is the only way a verdict lands inside the latency budget in
 * PRD §4. When the tree yields nothing usable — a layout change, a card drawn
 * to a canvas — [com.blacktop.capture.VisionFallback] takes a screenshot and
 * has the vision model read it instead. Slower, but it survives the change.
 *
 * ## What this service cannot do
 *
 * It overrides [onAccessibilityEvent] to *read* node text and nothing else. It
 * never calls `performAction`, `dispatchGesture`, or `performGlobalAction`, and
 * the manifest config requests `canRetrieveWindowContent` alone — no gesture
 * capability is declared, so the platform would refuse an injection attempt
 * even if one were coded. That is invariant I3, enforced where a reviewer can
 * check it without trusting this comment.
 *
 * `packageNames` in the config pins the service to delivery apps, so it is
 * never handed content from anything else on the phone.
 */
class OfferCaptureService : AccessibilityService() {

    /** Cards re-fire content-changed events constantly; scoring the same text
     *  repeatedly would spam the HUD and pollute the offer log. */
    private var lastSignature: String? = null
    private var lastEventAtMs = 0L

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        val app = application as? BlacktopApp ?: return
        if (!app.session.isOnline) return

        val now = System.currentTimeMillis()
        if (now - lastEventAtMs < DEBOUNCE_MS) return
        lastEventAtMs = now

        val platform = platformFor(event.packageName?.toString()) ?: return
        val root = rootInActiveWindow ?: return
        val text = try {
            collectText(root)
        } finally {
            @Suppress("DEPRECATION")
            root.recycle()
        }
        if (text.isBlank()) return

        // A card is only worth scoring once. Signature on the text itself
        // rather than a node id, since ids churn across app versions.
        val signature = text.hashCode().toString()
        if (signature == lastSignature) return

        val parsed = parseOfferText(
            text = text,
            offerId = "o-$now",
            nowEpochMinutes = now / 60_000L,
            platform = platform,
            knownCapValue = app.session.tips.capDetector.capValue,
            source = CaptureSource.ACCESSIBILITY_NODES,
        )

        if (!parsed.usable) {
            // Not necessarily a failure — most window updates are not offer
            // cards. Only escalate when this looked like a card we should have
            // read, so parse-failure telemetry stays meaningful (PRD §7).
            if (looksLikeAnOfferCard(text)) {
                app.telemetry.recordParseFailure(platform, parsed.failures, text.length)
                app.visionFallback.attempt(parsed.confidence)
            }
            return
        }

        lastSignature = signature
        val offer = parsed.offer ?: return
        app.session.scoreAndShow(offer, parsed.confidence)
    }

    override fun onInterrupt() {
        lastSignature = null
    }

    /**
     * Depth-first text collection, in the order the user sees it.
     *
     * Both `text` and `contentDescription` are read: cards put the payout in
     * one and the merchant in the other, inconsistently and differently across
     * app versions.
     */
    private fun collectText(node: AccessibilityNodeInfo, depth: Int = 0): String {
        if (depth > MAX_DEPTH) return ""
        val sb = StringBuilder()
        node.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { sb.append(it).append('\n') }
        node.contentDescription?.toString()?.trim()?.takeIf { it.isNotEmpty() }
            ?.let { sb.append(it).append('\n') }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            try {
                sb.append(collectText(child, depth + 1))
            } finally {
                @Suppress("DEPRECATION")
                child.recycle()
            }
        }
        return sb.toString()
    }

    private fun platformFor(pkg: String?): Platform? = when (pkg) {
        "com.dd.doordash" -> Platform.DOORDASH
        "com.ubercab.driver" -> Platform.UBER_EATS
        "com.grubhub.android.driver" -> Platform.GRUBHUB
        else -> null
    }

    /**
     * Distinguishes "this window is not an offer" from "this was an offer and
     * we failed to read it". Only the second is a parse failure worth alerting
     * on, and conflating them would bury a real layout change in noise.
     */
    private fun looksLikeAnOfferCard(text: String): Boolean =
        OFFER_MARKERS.count { it.containsMatchIn(text) } >= 2

    private companion object {
        const val DEBOUNCE_MS = 250L
        const val MAX_DEPTH = 40
        val OFFER_MARKERS = listOf(
            Regex("""\$\s?\d"""),
            Regex("""\d+(\.\d+)?\s*(mi|miles)\b""", RegexOption.IGNORE_CASE),
            Regex("""\d+\s*min\b""", RegexOption.IGNORE_CASE),
            Regex("""accept|decline""", RegexOption.IGNORE_CASE),
            Regex("""deliver\s+by""", RegexOption.IGNORE_CASE),
        )
    }
}
