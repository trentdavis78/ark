package com.blacktop.hud

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.IBinder
import android.speech.tts.TextToSpeech
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import com.blacktop.R
import com.blacktop.domain.Verdict
import com.blacktop.domain.VerdictColor
import java.util.Locale

/**
 * F12 — the driving-safe HUD.
 *
 * A floating overlay drawn over the delivery app, showing one colour and one
 * number, spoken aloud. PRD §9: if the driver has to read a sentence to know
 * what to do, it failed. The eyes belong on the road, so audio is the primary
 * channel and this is the fallback.
 *
 * The overlay is not touchable. FLAG_NOT_TOUCHABLE means every tap passes
 * straight through to the app underneath — so the HUD can never intercept, and
 * can never be mistaken for the accept button it sits near (I3/I4).
 */
class HudService : Service() {

    private lateinit var windowManager: WindowManager
    private var overlay: View? = null
    private var headline: TextView? = null
    private var compare: TextView? = null
    private var costs: TextView? = null
    private var tts: TextToSpeech? = null
    private var ttsReady = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        tts = TextToSpeech(this) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) tts?.language = Locale.US
        }
        startForeground(NOTIFICATION_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SHOW -> render(intent)
            ACTION_HIDE -> hide()
        }
        return START_STICKY
    }

    private fun render(intent: Intent) {
        val colorName = intent.getStringExtra(EXTRA_COLOR) ?: return
        val color = runCatching { VerdictColor.valueOf(colorName) }.getOrNull() ?: return
        ensureOverlay()

        val accent = when (color) {
            VerdictColor.GREEN -> Color.parseColor("#2ECC71")
            VerdictColor.AMBER -> Color.parseColor("#F5C518")
            VerdictColor.RED -> Color.parseColor("#FF4D4D")
            VerdictColor.MANUAL_FALLBACK -> Color.parseColor("#97A3AD")
        }
        val word = when (color) {
            VerdictColor.GREEN -> "TAKE IT"
            VerdictColor.AMBER -> "CLOSE"
            VerdictColor.RED -> "SKIP"
            VerdictColor.MANUAL_FALLBACK -> "YOUR CALL"
        }

        headline?.apply {
            text = word
            setTextColor(accent)
        }
        // Never a number without what it is compared against (PRD §9.5).
        compare?.text = intent.getStringExtra(EXTRA_COMPARE).orEmpty()
        costs?.text = intent.getStringExtra(EXTRA_COSTS).orEmpty()
        (overlay?.background as? GradientDrawable)?.setStroke(dp(3), accent)
        overlay?.visibility = View.VISIBLE

        intent.getStringExtra(EXTRA_TTS)?.let(::speak)
    }

    private fun speak(text: String) {
        if (!ttsReady) return
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "verdict")
    }

    private fun hide() {
        overlay?.visibility = View.GONE
    }

    private fun ensureOverlay() {
        if (overlay != null) return

        val pad = dp(16)
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, dp(12), pad, dp(12))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#F00B0D0F"))
                cornerRadius = dp(18).toFloat()
            }
        }
        headline = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 34f)
            setTypeface(typeface, Typeface.BOLD)
        }
        compare = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
            setTextColor(Color.parseColor("#F2F5F7"))
        }
        // The invisible costs are the whole reason the product exists, so they
        // are the second thing on the overlay (PRD §9.6).
        costs = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setTextColor(Color.parseColor("#97A3AD"))
        }
        container.addView(headline)
        container.addView(compare)
        container.addView(costs)

        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            // NOT_TOUCHABLE is the important one: taps pass through to the app
            // underneath, so this overlay can never intercept an accept.
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            y = dp(72)
        }

        runCatching { windowManager.addView(container, params) }
            .onSuccess { overlay = container }
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    private fun buildNotification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.hud_channel),
                                    NotificationManager.IMPORTANCE_LOW))
        }
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.hud_running))
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        overlay?.let { runCatching { windowManager.removeView(it) } }
        overlay = null
        tts?.shutdown()
        super.onDestroy()
    }

    companion object {
        const val ACTION_SHOW = "com.blacktop.hud.SHOW"
        const val ACTION_HIDE = "com.blacktop.hud.HIDE"
        const val EXTRA_COLOR = "color"
        const val EXTRA_COMPARE = "compare"
        const val EXTRA_COSTS = "costs"
        const val EXTRA_TTS = "tts"
        private const val CHANNEL_ID = "blacktop-hud"
        private const val NOTIFICATION_ID = 1

        fun show(context: Context, verdict: Verdict) {
            val tb = verdict.timeBreakdown
            val compare = if (verdict.isActionable) {
                "${Math.round(verdict.projectedNetHourly)} vs " +
                    "${Math.round(verdict.reservationRateHourly)}/hr"
            } else {
                "Couldn't read the card"
            }
            val costs = tb?.let {
                "wait ${Math.round(it.merchantWaitMin)}m · " +
                    "deadhead ${Math.round(it.returnToDensityMin)}m · " +
                    "tip est ${"%.2f".format(verdict.expectedHiddenTip)}"
            }.orEmpty()

            context.startService(Intent(context, HudService::class.java).apply {
                action = ACTION_SHOW
                putExtra(EXTRA_COLOR, verdict.color.name)
                putExtra(EXTRA_COMPARE, compare)
                putExtra(EXTRA_COSTS, costs)
                putExtra(EXTRA_TTS, verdict.ttsText)
            })
        }

        fun hide(context: Context) {
            context.startService(Intent(context, HudService::class.java).apply {
                action = ACTION_HIDE
            })
        }
    }
}
