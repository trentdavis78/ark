package com.blacktop.data

import com.blacktop.domain.Platform
import com.blacktop.domain.parseOfferText
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

private const val T0 = 29_000_000L

private val GOOD_CARD = """
    ${'$'}9.75
    Wendy's — Rt 17
    4.2 mi · 23 min
""".trimIndent()

class CaptureDiagnosticsTest {

    private fun parse(text: String) = parseOfferText(text, "o1", T0)

    @Test
    fun `records nothing while disabled`() {
        val d = CaptureDiagnostics()
        d.record(Platform.DOORDASH, GOOD_CARD, parse(GOOD_CARD))
        assertEquals(0, d.size)
    }

    @Test
    fun `records once enabled`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        d.record(Platform.DOORDASH, GOOD_CARD, parse(GOOD_CARD))
        assertEquals(1, d.size)
        val e = d.all().first()
        assertTrue(e.usable)
        assertEquals(9.75, e.payout)
        assertEquals(4.2, e.distanceMi)
    }

    @Test
    fun `keeps the raw text a failed parse could not handle`() {
        // The entire value of this class: a failure has to carry the input
        // that caused it, or it is not actionable.
        val d = CaptureDiagnostics().apply { enabled = true }
        val weird = "Offer\n9 dollars 75\n4 point 2 miles"
        d.record(Platform.DOORDASH, weird, parse(weird))
        val e = d.all().first()
        assertFalse(e.usable)
        assertEquals(weird, e.rawText)
        assertTrue("payout" in e.missingFields)
    }

    @Test
    fun `separates found fields from missing ones`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        val partial = "${'$'}9.75\nWendy's"
        d.record(Platform.DOORDASH, partial, parse(partial))
        val e = d.all().first()
        assertTrue("payout" in e.foundFields)
        assertTrue("distance" in e.missingFields)
        assertTrue("minutes" in e.missingFields)
    }

    @Test
    fun `newest capture is listed first`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        d.record(Platform.DOORDASH, "${'$'}1.00\nA\n1 mi · 1 min", parse("${'$'}1.00\nA\n1 mi · 1 min"))
        d.record(Platform.DOORDASH, "${'$'}2.00\nB\n2 mi · 2 min", parse("${'$'}2.00\nB\n2 mi · 2 min"))
        assertEquals(2.0, d.all().first().payout)
    }

    @Test
    fun `bounds memory under a broken layout`() {
        // A layout change fires failures continuously; this must not grow
        // without limit while the driver is mid-shift.
        val d = CaptureDiagnostics().apply { enabled = true }
        repeat(200) { d.record(Platform.DOORDASH, "junk $it", parse("junk $it")) }
        assertTrue(d.size <= 25, "expected a bounded buffer, got ${d.size}")
    }

    @Test
    fun `truncates a runaway text dump`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        val huge = "x".repeat(50_000)
        d.record(Platform.DOORDASH, huge, parse(huge))
        assertTrue(d.all().first().rawText.length <= 4_000)
    }

    @Test
    fun `clear empties the buffer`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        d.record(Platform.DOORDASH, GOOD_CARD, parse(GOOD_CARD))
        d.clear()
        assertEquals(0, d.size)
    }

    @Test
    fun `report carries the raw text and the parse outcome`() {
        val d = CaptureDiagnostics().apply { enabled = true }
        d.record(Platform.DOORDASH, GOOD_CARD, parse(GOOD_CARD))
        val report = d.report()
        assertTrue("Wendy's" in report, "raw text must survive into the report")
        assertTrue("OK" in report)
        assertTrue("DOORDASH" in report)
    }

    @Test
    fun `report is readable with nothing captured`() {
        assertTrue("entries: 0" in CaptureDiagnostics().report())
    }
}
