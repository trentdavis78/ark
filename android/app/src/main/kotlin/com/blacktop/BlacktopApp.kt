package com.blacktop

import android.app.Application
import com.blacktop.capture.ScreenCapture
import com.blacktop.capture.VisionFallback
import com.blacktop.data.DriverSession
import com.blacktop.data.OfferLog
import com.blacktop.data.Telemetry
import com.blacktop.hud.HudService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob

/**
 * Process-wide wiring.
 *
 * The accessibility service, the HUD, and the UI all need the same learned
 * models, and none of them may hold a separate copy — a tip estimator that
 * only the UI knows about is a tip estimator that never sees the offers.
 */
class BlacktopApp : Application() {

    lateinit var offerLog: OfferLog
        private set
    lateinit var session: DriverSession
        private set
    lateinit var telemetry: Telemetry
        private set
    lateinit var screenCapture: ScreenCapture
        private set
    lateinit var visionFallback: VisionFallback
        private set

    private val scope = CoroutineScope(SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        offerLog = OfferLog(this)
        session = DriverSession(this, offerLog)
        telemetry = Telemetry()
        screenCapture = ScreenCapture(this, scope)
        visionFallback = VisionFallback(
            screenCapture = screenCapture,
            onOffer = { offer, confidence -> session.scoreAndShow(offer, confidence) },
            onUnavailable = { message -> HudService.show(this, unreadableVerdict(message)) },
        )
        session.hydrate()
    }

    /**
     * When neither path could read the card, the driver is told exactly that —
     * with no number attached. A confident-looking figure from a failed read is
     * the one output that would actively cost them money.
     */
    private fun unreadableVerdict(message: String) = com.blacktop.domain.Verdict(
        color = com.blacktop.domain.VerdictColor.MANUAL_FALLBACK,
        projectedNetHourly = 0.0,
        reservationRateHourly = 0.0,
        expectedPayout = 0.0,
        expectedHiddenTip = 0.0,
        timeBreakdown = null,
        parseConfidence = 0.0,
        ttsText = message,
    )
}
