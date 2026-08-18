package com.blacktop.domain

import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

private const val T0 = 29_000_000L   // arbitrary epoch-minute anchor

private fun offer(
    payout: Double = 7.0,
    miles: Double? = 3.0,
    minutes: Double? = 20.0,
    capped: Boolean = false,
    merchant: String = "Wendy's — Rt 17",
) = Offer(
    offerId = "o1",
    platform = Platform.DOORDASH,
    seenAtEpochMinutes = T0,
    displayedPayout = payout,
    merchantName = merchant,
    merchantId = merchant,
    statedDistanceMi = miles,
    statedMinutes = minutes,
    hitDisplayCap = capped,
)

private fun rate(hourly: Double) = RateDecision(
    wStarPerMin = hourly / 60.0, hourly = hourly, rawWPerMin = hourly / 60.0,
    decayFactor = 1.0, arAdjustmentPerMin = 0.0, outOfBand = false,
    floorApplied = false, arrivalRatePerMin = 0.3, sampleN = 20,
)

class DisplayCapDetectorTest {
    @Test fun `stays silent until a figure repeats enough`() {
        val d = DisplayCapDetector(minRepeats = 5)
        repeat(4) { d.observe(12.00) }
        assertNull(d.capValue)
    }

    @Test fun `detects the modal repeated figure`() {
        val d = DisplayCapDetector(minRepeats = 3)
        repeat(6) { d.observe(12.00) }
        listOf(5.25, 7.10, 9.40).forEach { d.observe(it) }
        assertEquals(12.00, d.capValue)
        assertTrue(d.isAtCap(12.00))
        assertFalse(d.isAtCap(11.75))
    }

    @Test fun `ignores non-positive payouts`() {
        val d = DisplayCapDetector(minRepeats = 2)
        repeat(5) { d.observe(0.0) }
        assertNull(d.capValue)
    }
}

class TipEstimatorTest {
    @Test fun `falls back to the market prior with no history`() {
        assertEquals(3.5, TipEstimator(marketPriorTip = 3.5).expectedTip(offer()), 1e-9)
    }

    @Test fun `merchant history pulls the estimate off the prior`() {
        val est = TipEstimator(marketPriorTip = 3.5, shrinkageK = 2.0)
        val o = offer()
        repeat(10) { est.observeDelivery(o, actualPayout = o.displayedPayout + 9.0) }
        val tip = est.expectedTip(o)
        assertTrue(tip > 6.0, "expected merchant history to dominate, got $tip")
        assertTrue(tip < 9.0, "prior should still shrink it, got $tip")
    }

    @Test fun `capped offers never estimate below uncapped`() {
        val est = TipEstimator(marketPriorTip = 3.5)
        assertTrue(est.expectedTip(offer(payout = 12.0, capped = true)) >=
                   est.expectedTip(offer(payout = 12.0)))
    }

    @Test fun `capped labels do not pollute the base distribution`() {
        val est = TipEstimator(marketPriorTip = 3.5, shrinkageK = 1.0)
        val capped = offer(payout = 12.0, capped = true, merchant = "A")
        repeat(10) { est.observeDelivery(capped, actualPayout = 30.0) }
        assertEquals(3.5, est.expectedTip(offer(merchant = "B")), 0.01)
    }

    @Test fun `counts labels from both paths`() {
        val est = TipEstimator()
        est.observeDelivery(offer(), 10.0)
        est.observeDelivery(offer(capped = true), 30.0)
        assertEquals(2, est.labelCount)
    }
}

class RollingWindowTest {
    @Test fun `prefers fresh observations when the window has enough`() {
        val w = RollingOfferWindow(windowMinutes = 20.0, minSamples = 3)
        w.observe(OfferObservation(T0, 10.0, 20.0))
        repeat(3) { w.observe(OfferObservation(T0 + 25 + it, 10.0, 20.0)) }
        val snap = w.snapshot(T0 + 27)
        assertEquals(3, snap.size)
        assertTrue(snap.all { it.seenAtEpochMinutes > T0 })
    }

    @Test fun `reaches back when the window is starved`() {
        // At high utilization a strict window holds two or three samples and
        // the optimal-stopping solve never runs.
        val w = RollingOfferWindow(windowMinutes = 20.0, minSamples = 4)
        repeat(4) { w.observe(OfferObservation(T0 + it, 10.0, 20.0)) }
        w.observe(OfferObservation(T0 + 90, 10.0, 20.0))
        assertEquals(4, w.snapshot(T0 + 92).size)
    }

    @Test fun `evicts beyond the retention horizon`() {
        val w = RollingOfferWindow(windowMinutes = 20.0, minSamples = 2, retainMinutes = 60.0)
        w.observe(OfferObservation(T0, 10.0, 20.0))
        w.observe(OfferObservation(T0 + 200, 10.0, 20.0))
        assertEquals(1, w.snapshot(T0 + 200).size)
    }

    @Test fun `arrival rate uses the real span when reaching back`() {
        val w = RollingOfferWindow(windowMinutes = 30.0, minSamples = 5)
        repeat(5) { w.observe(OfferObservation(T0 + it * 25L, 10.0, 20.0)) }
        assertEquals(5 / 100.0, w.arrivalRatePerMin(T0 + 100), 1e-9)
    }

    @Test fun `busy time is excluded from the lambda denominator`() {
        // Two offers over 60 elapsed minutes, 40 spent on a delivery, is
        // 2/20 per available minute — not 2/60.
        val w = RollingOfferWindow(windowMinutes = 60.0, minSamples = 2)
        w.observe(OfferObservation(T0, 10.0, 20.0))
        w.observe(OfferObservation(T0 + 60, 10.0, 20.0))
        w.noteUnavailable(T0 + 5, T0 + 45)
        assertEquals(2 / 20.0, w.arrivalRatePerMin(T0 + 60), 1e-9)
    }

    @Test fun `rejects an out-of-range window`() {
        try {
            RollingOfferWindow(windowMinutes = 5.0)
            throw AssertionError("expected IllegalArgumentException")
        } catch (_: IllegalArgumentException) {
        }
    }
}

class SolveWStarTest {
    @Test fun `empty input yields zero`() {
        assertEquals(0.0, solveWStar(emptyList(), 0.5))
        assertEquals(0.0, solveWStar(listOf(OfferObservation(T0, 10.0, 20.0)), 0.0))
    }

    @Test fun `single offer type matches the closed form`() {
        val obs = (0 until 10).map { OfferObservation(T0 + it * 2L, 12.0, 20.0) }
        assertEquals(12.0 / 22.0, solveWStar(obs, 0.5), 1e-9)
    }

    @Test fun `selectivity beats accepting everything`() {
        val obs = (0 until 20).map {
            if (it % 2 == 0) OfferObservation(T0 + it.toLong(), 4.0, 20.0)
            else OfferObservation(T0 + it.toLong(), 20.0, 20.0)
        }
        val acceptAll = obs.sumOf { it.valueDollars } / obs.size / 21.0
        assertTrue(solveWStar(obs, 1.0) > acceptAll)
    }
}

class ReservationRateEngineTest {
    @Test fun `cold start uses the prior`() {
        val d = ReservationRateEngine(priorWPerMin = 0.5).currentRate(T0)
        assertEquals(0.5, d.wStarPerMin, 1e-9)
        assertEquals(0, d.sampleN)
        assertTrue(d.notes.any { "cold start" in it })
    }

    @Test fun `a thin window pools instead of snapping to the prior`() {
        val eng = ReservationRateEngine(priorWPerMin = 0.50, minSamples = 5)
        repeat(3) { eng.observe(OfferObservation(T0 + it * 8L, 6.0, 30.0)) }
        val d = eng.currentRate(T0 + 20)
        assertTrue(d.wStarPerMin < 0.50, "expected pooling toward the evidence")
        assertTrue(d.notes.any { "thin window" in it })
    }

    @Test fun `a learned prior overrides the constant`() {
        val eng = ReservationRateEngine(priorWPerMin = 0.50, priorProvider = { 0.22 })
        assertEquals(0.22, eng.currentRate(T0).wStarPerMin, 1e-9)
    }

    @Test fun `the sanity band flags but never overrides`() {
        // Clamping would reinstate the fixed threshold F6 exists to replace.
        val eng = ReservationRateEngine()
        repeat(30) { eng.observe(OfferObservation(T0 + it / 2L, 40.0, 10.0)) }
        val d = eng.currentRate(T0 + 15)
        assertTrue(d.outOfBand)
        assertTrue(d.wStarPerMin > SANITY_BAND_PER_MIN.endInclusive)
        assertTrue(d.notes.any { "outside sanity band" in it })
    }

    @Test fun `a lean market is not floored up to the band`() {
        val eng = ReservationRateEngine()
        repeat(30) { eng.observe(OfferObservation(T0 + it * 2L, 5.4, 30.0)) }
        val d = eng.currentRate(T0 + 58)
        assertTrue(d.wStarPerMin < SANITY_BAND_PER_MIN.start)
        assertFalse(d.floorApplied)
    }

    @Test fun `the marginal cost floor binds`() {
        val eng = ReservationRateEngine(marginalCostPerMin = 0.25)
        repeat(30) { eng.observe(OfferObservation(T0 + it * 2L, 3.0, 30.0)) }
        val d = eng.currentRate(T0 + 58)
        assertTrue(d.floorApplied)
        assertEquals(0.25, d.wStarPerMin, 1e-9)
    }

    @Test fun `contexts do not contaminate each other`() {
        // One shared window let a dense corridor set a sparse zone's threshold,
        // collapsing sparse acceptance to 4%.
        val eng = ReservationRateEngine(minSamples = 3)
        repeat(20) { eng.observe(OfferObservation(T0 + it.toLong(), 20.0, 20.0), "dense") }
        repeat(20) { eng.observe(OfferObservation(T0 + it.toLong(), 5.0, 40.0), "sparse") }
        val dense = eng.currentRate(T0 + 20, context = "dense")
        val sparse = eng.currentRate(T0 + 20, context = "sparse")
        assertTrue(dense.wStarPerMin > sparse.wStarPerMin * 2,
            "dense ${dense.wStarPerMin} vs sparse ${sparse.wStarPerMin}")
    }

    @Test fun `session end decay lowers the rate`() {
        val eng = ReservationRateEngine()
        repeat(30) { eng.observe(OfferObservation(T0 + it.toLong(), 9.0, 20.0)) }
        val base = eng.currentRate(T0 + 27)
        val late = eng.currentRate(T0 + 27, remainingSessionMinutes = 10.0)
        assertTrue(late.wStarPerMin <= base.wStarPerMin)
        assertEquals(10.0 / 45.0, late.decayFactor, 1e-9)
    }

    @Test fun `ar farming only applies in sparse zones near month end`() {
        assertEquals(0.0, arFarmingAdjustment(0.6, null).first)
        assertEquals(0.0, arFarmingAdjustment(0.6,
            ArPolicyInputs(0.44, 0.50, 2, zoneIsSparse = false)).first)
        assertTrue(arFarmingAdjustment(0.6,
            ArPolicyInputs(0.44, 0.50, 2, zoneIsSparse = true)).first < 0.0)
    }
}

class CompletionRiskTest {
    @Test fun `open ended sessions are never risky`() = assertFalse(completionRisk(90.0, null))
    @Test fun `fits inside the grace period`() =
        assertFalse(completionRisk(50.0, 40.0, graceMinutes = 20.0))
    @Test fun `too long to finish`() =
        assertTrue(completionRisk(70.0, 40.0, graceMinutes = 20.0))
}

class VerdictEngineTest {
    @Test fun `a low confidence parse yields no verdict`() {
        val v = VerdictEngine().evaluate(offer(), T0, rate(30.0), parseConfidence = 0.5)
        assertEquals(VerdictColor.MANUAL_FALLBACK, v.color)
        assertFalse(v.isActionable)
        assertNull(v.timeBreakdown)
    }

    @Test fun `the confidence boundary is inclusive`() {
        assertTrue(VerdictEngine().evaluate(offer(), T0, rate(30.0),
            parseConfidence = MIN_PARSE_CONFIDENCE).isActionable)
    }

    @Test fun `a rich offer reads green`() {
        val v = VerdictEngine().evaluate(offer(payout = 22.0), T0, rate(20.0))
        assertEquals(VerdictColor.GREEN, v.color)
        assertTrue("Take it" in v.ttsText)
    }

    @Test fun `a poor offer reads red`() {
        assertEquals(VerdictColor.RED,
            VerdictEngine().evaluate(offer(payout = 3.0), T0, rate(40.0)).color)
    }

    @Test fun `the indifference band reads amber`() {
        val e = VerdictEngine()
        val o = offer(payout = 10.0)
        val hourly = 10.0 / e.buildTimeBreakdown(o, T0).totalMinutes * 60.0
        assertEquals(VerdictColor.AMBER, e.evaluate(o, T0, rate(hourly)).color)
    }

    @Test fun `completion risk overrides a green`() {
        val v = VerdictEngine().evaluate(offer(payout = 40.0), T0, rate(10.0),
            remainingSessionMinutes = 5.0)
        assertEquals(VerdictColor.RED, v.color)
        assertTrue("Won't finish" in v.ttsText)
    }

    @Test fun `the tip estimate moves the verdict`() {
        val o = offer(payout = 6.0)
        val without = VerdictEngine().evaluate(o, T0, rate(25.0))
        val withTip = VerdictEngine(tipEstimator = { 9.0 }).evaluate(o, T0, rate(25.0))
        assertTrue(withTip.projectedNetHourly > without.projectedNetHourly)
        assertEquals(9.0, withTip.expectedHiddenTip, 1e-9)
    }

    @Test fun `a negative tip estimate is floored at zero`() {
        assertEquals(0.0, VerdictEngine(tipEstimator = { -5.0 })
            .evaluate(offer(), T0, rate(25.0)).expectedHiddenTip, 1e-9)
    }

    @Test fun `the deadhead term is part of the denominator`() {
        val near = VerdictEngine(returnToDensity = { _, _ -> 2.0 })
            .evaluate(offer(), T0, rate(25.0))
        val far = VerdictEngine(returnToDensity = { _, _ -> 25.0 })
            .evaluate(offer(), T0, rate(25.0))
        assertTrue(far.projectedNetHourly < near.projectedNetHourly)
    }

    @Test fun `a null route estimate falls back rather than counting as zero`() {
        val provider = object : RouteProvider {
            override fun driveToMerchantMinutes(offer: Offer): Double? = null
            override fun driveToCustomerMinutes(offer: Offer): Double = 12.0
        }
        val tb = VerdictEngine(routeProvider = provider).buildTimeBreakdown(offer(), T0)
        assertTrue(tb.driveToMerchantMin > 0.0)
        assertEquals(12.0, tb.driveToCustomerMin, 1e-9)
    }

    @Test fun `an offer with no distance or time still gets an estimate`() {
        val tb = VerdictEngine().buildTimeBreakdown(offer(miles = null, minutes = null), T0)
        assertTrue(tb.totalMinutes > 15.0)
    }

    @Test fun `the headline shows what the number is compared against`() {
        assertTrue("vs" in VerdictEngine()
            .evaluate(offer(payout = 22.0), T0, rate(20.0)).headline())
    }
}

class MerchantWaitOracleTest {
    private fun dwell(o: MerchantWaitOracle, id: String, minutes: Long, at: Long = T0) =
        o.observeDwell(id, at, at + minutes)

    @Test fun `defaults before it has evidence`() {
        val est = MerchantWaitOracle().waitEstimate("m1", T0)
        assertEquals("default", est.source)
        assertEquals(0, est.sampleN)
    }

    @Test fun `prefers the hour bucket once dense enough`() {
        val o = MerchantWaitOracle()
        repeat(5) { dwell(o, "m1", 12) }
        val est = o.waitEstimate("m1", T0)
        assertEquals("hour", est.source)
        assertEquals(12.0, est.p50, 1e-9)
    }

    @Test fun `clips an implausible dwell`() {
        val o = MerchantWaitOracle()
        repeat(5) { dwell(o, "m1", 500) }
        assertTrue(o.waitEstimate("m1", T0).p50 <= 60.0)
    }

    @Test fun `flags a chronic offender`() {
        val o = MerchantWaitOracle()
        repeat(5) { dwell(o, "slow", 14) }
        assertTrue(o.chronicOffender("slow", T0))
        assertTrue(assertNotNull(o.flagText("slow", T0)).contains("14 min"))
    }

    @Test fun `advises arriving later but never past the deadline`() {
        val o = MerchantWaitOracle()
        repeat(5) { dwell(o, "slow", 14) }
        assertEquals(12.0, o.arrivalDelayAdvice("slow", T0, 5.0), 1e-9)
        assertEquals(3.0, o.arrivalDelayAdvice("slow", T0, 5.0, 10.0), 1e-9)
        assertEquals(0.0, o.arrivalDelayAdvice("slow", T0, 5.0, 0.0), 1e-9)
    }

    @Test fun `rejects a departure before arrival`() {
        try {
            MerchantWaitOracle().observeDwell("m", T0 + 10, T0)
            throw AssertionError("expected IllegalArgumentException")
        } catch (_: IllegalArgumentException) {
        }
    }
}

class OfferParserTest {
    private val card = """
        ${'$'}9.75
        Wendy's — Rt 17
        Deliver by 7:42 PM
        4.2 mi · 23 min
    """.trimIndent()

    @Test fun `parses a well formed card`() {
        val p = parseOfferText(card, "o1", T0)
        assertTrue(p.usable)
        val o = assertNotNull(p.offer)
        assertEquals(9.75, o.displayedPayout, 1e-9)
        assertEquals(4.2, assertNotNull(o.statedDistanceMi), 1e-9)
        assertEquals(23.0, assertNotNull(o.statedMinutes), 1e-9)
        assertEquals("Wendy's — Rt 17", o.merchantName)
        assertEquals(CaptureSource.ACCESSIBILITY_NODES, o.source)
    }

    @Test fun `garbage text scores below the gate`() {
        val p = parseOfferText("lorem ipsum dolor sit amet", "o1", T0)
        assertFalse(p.usable)
        assertNull(p.offer)
        assertTrue("payout_not_found" in p.failures)
    }

    @Test fun `a missing payout is never usable`() {
        val p = parseOfferText("Wendy's\n4.2 mi · 23 min", "o1", T0)
        assertFalse(p.usable)
        assertTrue(p.confidence < MIN_PARSE_CONFIDENCE)
    }

    @Test fun `peak pay is not mistaken for the payout`() {
        val p = parseOfferText(
            "Peak Pay +${'$'}2.00\n${'$'}8.50\nChipotle\n2.0 mi · 15 min", "o1", T0)
        assertEquals(8.50, assertNotNull(p.offer).displayedPayout, 1e-9)
    }

    @Test fun `a may-be-higher disclaimer marks the cap`() {
        val p = parseOfferText(
            "${'$'}12.00\nTotal may be higher\nCheesecake\n3.0 mi · 20 min", "o1", T0)
        assertTrue(assertNotNull(p.offer).hitDisplayCap)
    }

    @Test fun `a known cap value marks the cap`() {
        val p = parseOfferText(card.replace("${'$'}9.75", "${'$'}12.00"), "o1", T0,
            knownCapValue = 12.00)
        assertTrue(assertNotNull(p.offer).hitDisplayCap)
    }

    @Test fun `detects shop and deliver`() {
        val p = parseOfferText(
            "${'$'}18.00\nShop & Deliver\nShopRite\n2.0 mi · 30 min", "o1", T0)
        assertEquals(OfferType.SHOP_DELIVER, assertNotNull(p.offer).offerType)
    }

    @Test fun `records the vision source when told to`() {
        val p = parseOfferText(card, "o1", T0, source = CaptureSource.VISION)
        assertEquals(CaptureSource.VISION, assertNotNull(p.offer).source)
    }

    @Test fun `manual entry is fully trusted`() {
        val p = manualOffer(11.50, 5.0, 25.0, "m1", T0)
        assertTrue(p.usable)
        assertEquals(1.0, p.confidence, 1e-9)
        assertEquals(Platform.MANUAL, assertNotNull(p.offer).platform)
        assertEquals(CaptureSource.MANUAL, assertNotNull(p.offer).source)
    }

    @Test fun `field confidence is reported for telemetry`() {
        val p = parseOfferText("${'$'}9.75\nWendy's", "o1", T0)
        assertEquals(1.0, p.fieldConfidence.getValue("payout"), 1e-9)
        assertEquals(0.0, p.fieldConfidence.getValue("distance"), 1e-9)
    }
}

class HourOfWeekTest {
    @Test fun `covers the full week`() {
        assertEquals(168, (0 until 168).map { hourOfWeek(T0 + it * 60L) }.toSet().size)
    }

    @Test fun `is stable across whole weeks`() {
        assertEquals(hourOfWeek(T0), hourOfWeek(T0 + 7 * 24 * 60))
    }

    @Test fun `respects a zone offset`() {
        assertTrue(abs(hourOfWeek(T0) - hourOfWeek(T0, zoneOffsetMinutes = -300)) > 0)
    }
}
