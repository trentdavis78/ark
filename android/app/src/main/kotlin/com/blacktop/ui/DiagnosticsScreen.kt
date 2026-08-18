package com.blacktop.ui

import android.content.Context
import android.content.Intent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.blacktop.BlacktopApp
import com.blacktop.data.CaptureDiagnostics

/**
 * "What did the reader actually see?"
 *
 * The parser's regexes have never met a real DoorDash accessibility tree. This
 * screen shows the raw text each capture produced next to what the parser made
 * of it, so a failure becomes a specific diff instead of a shrug.
 *
 * Turn it on, open DoorDash, wait for one offer, come back here.
 */
@Composable
fun DiagnosticsScreen(app: BlacktopApp, onBack: () -> Unit) {
    val context = androidx.compose.ui.platform.LocalContext.current
    var enabled by remember { mutableStateOf(app.diagnostics.enabled) }
    var entries by remember { mutableStateOf(app.diagnostics.all()) }

    Column(
        Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Spacer(Modifier.height(16.dp))
        Text("Reader diagnostics", fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text(
            "Records the raw text the accessibility tree hands us for each card, " +
            "and what the parser got out of it. Works while parked — you do not " +
            "need to be online.",
            color = Color(0xFF97A3AD), fontSize = 13.sp,
        )

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text("Record captures", fontWeight = FontWeight.Bold)
                        Text("Holds screen text in memory. Off by default.",
                             color = Color(0xFF97A3AD), fontSize = 12.sp)
                    }
                    Switch(checked = enabled, onCheckedChange = {
                        enabled = it
                        app.diagnostics.enabled = it
                    })
                }
                Text(
                    if (enabled) {
                        "Recording. Open DoorDash, let one offer appear, then come back."
                    } else {
                        "Not recording."
                    },
                    color = if (enabled) Color(0xFF2ECC71) else Color(0xFF97A3AD),
                    fontSize = 13.sp,
                )
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(
                onClick = { entries = app.diagnostics.all() },
                modifier = Modifier.weight(1f),
            ) { Text("Refresh (${app.diagnostics.size})") }
            OutlinedButton(
                onClick = {
                    app.diagnostics.clear()
                    entries = emptyList()
                },
                modifier = Modifier.weight(1f),
            ) { Text("Clear") }
        }

        if (entries.isNotEmpty()) {
            Button(
                onClick = { shareReport(context, app.diagnostics.report()) },
                modifier = Modifier.fillMaxWidth().height(50.dp),
            ) { Text("Share report", fontWeight = FontWeight.Bold) }
            Text(
                "Read it before you send it — it contains the text that was on your " +
                "screen, which includes the dropoff area.",
                color = Color(0xFFF5C518), fontSize = 12.sp,
            )
        }

        if (entries.isEmpty()) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Nothing captured yet", fontWeight = FontWeight.Bold)
                    Text(
                        "If this stays empty after an offer appears, the accessibility " +
                        "service is not receiving events — check it is enabled in " +
                        "Settings, and that DoorDash is the app in the foreground.",
                        color = Color(0xFF97A3AD), fontSize = 13.sp,
                    )
                }
            }
        }

        entries.forEach { CaptureCard(it) }
        Spacer(Modifier.height(12.dp))
        OutlinedButton(onClick = onBack, modifier = Modifier.fillMaxWidth()) { Text("Back") }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun CaptureCard(entry: CaptureDiagnostics.Entry) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    if (entry.usable) "PARSED" else "FAILED",
                    color = if (entry.usable) Color(0xFF2ECC71) else Color(0xFFFF4D4D),
                    fontWeight = FontWeight.Bold, fontSize = 13.sp,
                )
                Text("confidence ${"%.2f".format(entry.confidence)}",
                     color = Color(0xFF97A3AD), fontSize = 13.sp)
                Text(entry.platform.name, color = Color(0xFF97A3AD), fontSize = 13.sp)
            }

            if (entry.missingFields.isNotEmpty()) {
                Text("missing: ${entry.missingFields.joinToString(", ")}",
                     color = Color(0xFFF5C518), fontSize = 12.sp)
            }
            Text(
                "payout=${entry.payout ?: "—"}  " +
                "mi=${entry.distanceMi ?: "—"}  " +
                "min=${entry.minutes ?: "—"}  " +
                "merchant=${entry.merchant ?: "—"}",
                fontSize = 12.sp, fontFamily = FontFamily.Monospace,
            )

            Text("raw text:", color = Color(0xFF97A3AD), fontSize = 11.sp)
            Text(
                entry.rawText.ifBlank { "(the tree returned no text at all)" },
                fontSize = 11.sp,
                fontFamily = FontFamily.Monospace,
                color = Color(0xFFC7D0D8),
            )
        }
    }
}

private fun shareReport(context: Context, report: String) {
    val intent = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_SUBJECT, "BLACKTOP capture diagnostics")
        putExtra(Intent.EXTRA_TEXT, report)
    }
    context.startActivity(Intent.createChooser(intent, "Share diagnostics"))
}
