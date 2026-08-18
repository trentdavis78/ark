package com.blacktop.ui

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.blacktop.BlacktopApp
import com.blacktop.capture.OfferCaptureService
import com.blacktop.domain.manualOffer
import com.blacktop.hud.HudService

/**
 * The control surface. Not the product — the product is the HUD, which appears
 * over the delivery app. This screen exists to get permissions granted, start
 * and stop a session, show the numbers that reframe the work, and provide the
 * manual fallback.
 */
class MainActivity : ComponentActivity() {

    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val app = application as BlacktopApp
        app.screenCapture.onConsentResult(result.resultCode, result.data)
        app.visionFallback.enabled = app.screenCapture.isGranted
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = application as BlacktopApp
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                primary = Color(0xFF4DA3FF),
                background = Color(0xFF0B0D0F),
                surface = Color(0xFF14181C),
            )) {
                var consented by remember { mutableStateOf(readConsent()) }
                if (!consented) {
                    ConsentScreen(onAccept = {
                        writeConsent()
                        consented = true
                    })
                } else {
                    HomeScreen(
                        app = app,
                        onOpenAccessibilitySettings = {
                            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                        },
                        onOpenOverlaySettings = {
                            startActivity(Intent(
                                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                Uri.parse("package:$packageName")))
                        },
                        onEnableVision = {
                            projectionLauncher.launch(app.screenCapture.consentIntent())
                        },
                    )
                }
            }
        }
    }

    private fun prefs() = getSharedPreferences("blacktop", Context.MODE_PRIVATE)
    private fun readConsent() = prefs().getBoolean(KEY_CONSENT, false)
    private fun writeConsent() = prefs().edit().putBoolean(KEY_CONSENT, true).apply()

    private companion object { const val KEY_CONSENT = "informed_consent_v1" }
}

/**
 * PRD §11 requires informed consent in plain language, "not buried in an EULA".
 * This screen states what the app reads, what it cannot do, and the residual
 * risk — before anything is enabled.
 */
@Composable
private fun ConsentScreen(onAccept: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Spacer(Modifier.height(24.dp))
        Text("Before you turn this on", fontSize = 26.sp, fontWeight = FontWeight.Bold)

        Text(
            "BLACKTOP reads the offer card that is already on your screen and tells you " +
            "what it is worth per hour after the costs the card hides — the wait at the " +
            "counter and the drive back from a dead-end dropoff.",
            color = Color(0xFFC7D0D8),
        )

        SectionTitle("What it never does")
        Bullet("It never asks for your DoorDash login, and never stores one.")
        Bullet("It never talks to DoorDash, Uber, or Grubhub servers.")
        Bullet("It never taps, accepts, or declines. Every decision is yours.")
        Bullet("It never reads anything outside your delivery apps.")

        SectionTitle("What you should know")
        Text(
            "Reading the offer card on your own device is not something any delivery " +
            "platform has explicitly permitted. It touches none of their servers and uses " +
            "none of your credentials, which makes it different from tools that have been " +
            "shut down — but it is not blessed either, and you should decide with that in " +
            "front of you.",
            color = Color(0xFFC7D0D8),
        )
        Text(
            "If you would rather not enable screen reading at all, the app still works: " +
            "type the offer in yourself and it scores it exactly the same.",
            color = Color(0xFFC7D0D8),
        )

        Spacer(Modifier.height(8.dp))
        Button(onClick = onAccept, modifier = Modifier.fillMaxWidth().height(56.dp)) {
            Text("I understand", fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun HomeScreen(
    app: BlacktopApp,
    onOpenAccessibilitySettings: () -> Unit,
    onOpenOverlaySettings: () -> Unit,
    onEnableVision: () -> Unit,
) {
    val context = LocalContextCompat()
    var online by remember { mutableStateOf(app.session.isOnline) }
    var tick by remember { mutableIntStateOf(0) }

    Column(
        Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Spacer(Modifier.height(16.dp))
        Text("BLACKTOP", fontSize = 13.sp, letterSpacing = 3.sp, color = Color(0xFF97A3AD))

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(if (online) "Online" else "Offline",
                     fontSize = 22.sp, fontWeight = FontWeight.Bold)
                Text("${app.session.offersSeen} offers scored · " +
                     "${(app.session.acceptanceRate * 100).toInt()}% accepted",
                     color = Color(0xFF97A3AD))
                Button(
                    onClick = {
                        if (online) app.session.goOffline() else app.session.goOnline(null)
                        online = app.session.isOnline
                        tick++
                    },
                    modifier = Modifier.fillMaxWidth().height(54.dp),
                ) { Text(if (online) "Go offline" else "Go online", fontWeight = FontWeight.Bold) }
            }
        }

        SetupCard(
            title = "Offer reader",
            body = "Reads the card from your delivery app's own screen content. " +
                   "This is the fast path — no screenshot, no round trip.",
            action = "Enable in Accessibility settings",
            onClick = onOpenAccessibilitySettings,
        )
        SetupCard(
            title = "Verdict overlay",
            body = "Draws the verdict over the delivery app. Taps pass straight through — " +
                   "it cannot intercept your accept button.",
            action = "Allow drawing over other apps",
            onClick = onOpenOverlaySettings,
        )
        SetupCard(
            title = "Vision fallback (optional)",
            body = "When the card layout changes and the reader breaks, BLACKTOP can " +
                   "screenshot the card and have Claude read it instead. Slower, but it " +
                   "survives a redesign.",
            action = "Enable screen capture",
            onClick = onEnableVision,
        )

        if (app.telemetry.layoutLikelyChanged()) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Reader is struggling", fontWeight = FontWeight.Bold,
                         color = Color(0xFFF5C518))
                    Text(app.telemetry.summary(), color = Color(0xFF97A3AD), fontSize = 13.sp)
                    Text("The card layout may have changed. Enable the vision fallback, or " +
                         "enter offers by hand until it is fixed.",
                         color = Color(0xFF97A3AD), fontSize = 13.sp)
                }
            }
        }

        ManualEntryCard(app)

        Text("Advisory only. BLACKTOP never taps anything.",
             color = Color(0xFF97A3AD), fontSize = 12.sp,
             modifier = Modifier.align(Alignment.CenterHorizontally))
        Spacer(Modifier.height(24.dp))
    }
}

/**
 * I6 — manual entry ships in v1 and works standalone.
 *
 * This is the hedge for the whole product: if screen reading breaks or the
 * driver never enables it, the engine still works. It is deliberately on the
 * main screen rather than buried.
 */
@Composable
private fun ManualEntryCard(app: BlacktopApp) {
    var payout by remember { mutableStateOf("") }
    var miles by remember { mutableStateOf("") }
    var minutes by remember { mutableStateOf("") }
    val context = LocalContextCompat()

    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Enter an offer by hand", fontWeight = FontWeight.Bold)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(payout, { payout = it }, label = { Text("$") },
                                  modifier = Modifier.weight(1f), singleLine = true)
                OutlinedTextField(miles, { miles = it }, label = { Text("mi") },
                                  modifier = Modifier.weight(1f), singleLine = true)
                OutlinedTextField(minutes, { minutes = it }, label = { Text("min") },
                                  modifier = Modifier.weight(1f), singleLine = true)
            }
            Button(
                onClick = {
                    val p = payout.toDoubleOrNull() ?: return@Button
                    val parsed = manualOffer(
                        displayedPayout = p,
                        statedDistanceMi = miles.toDoubleOrNull(),
                        statedMinutes = minutes.toDoubleOrNull(),
                        offerId = "m-${System.currentTimeMillis()}",
                        nowEpochMinutes = System.currentTimeMillis() / 60_000L,
                    )
                    parsed.offer?.let { app.session.scoreAndShow(it, parsed.confidence) }
                    payout = ""; miles = ""; minutes = ""
                },
                modifier = Modifier.fillMaxWidth().height(50.dp),
            ) { Text("Score it") }
        }
    }
}

@Composable
private fun SetupCard(title: String, body: String, action: String, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, fontWeight = FontWeight.Bold)
            Text(body, color = Color(0xFF97A3AD), fontSize = 13.sp)
            OutlinedButton(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
                Text(action)
            }
        }
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, fontWeight = FontWeight.Bold, fontSize = 15.sp,
         modifier = Modifier.padding(top = 8.dp))
}

@Composable
private fun Bullet(text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("•", color = Color(0xFF2ECC71))
        Text(text, color = Color(0xFFC7D0D8))
    }
}

@Composable
private fun LocalContextCompat(): Context = androidx.compose.ui.platform.LocalContext.current
