# Compliance: the invariants, and how the code holds them

PRD §5.1 defines six non-negotiable invariants. This states, for each, where
the codebase enforces it. A diff that weakens one is release-blocking.

The reasoning behind them is worth restating, because it explains the shape of
the whole product. Para's tip-transparency feature took drivers' DoorDash
credentials and parsed the payout out of the JSON sent to the device. DoorDash
removed the payout from the payload and the product never recovered. The lesson
is not "don't do useful things" — it is that any capability depending on a
platform's cooperation, credentials, or server responses can be revoked
unilaterally and without notice.

## Where this build stands

The PRD accepts one residual risk, and this build takes it: on-device reading
of the offer card via accessibility services is a gray zone. It touches no
platform server and uses no credentials, which is materially different from
what got Para shut down — but it is not blessed by any platform's terms
either.

The mitigations are the ones PRD §5.1 specifies. The consent screen states the
risk in plain language before anything is enabled, not buried in an EULA.
Manual entry (I6) ships on the main screen and works standalone, so a driver
who declines screen reading still has the product. And every capability that
could escalate this from "reads my screen" to "acts for me" is refused at the
manifest level, where a reviewer can verify it without trusting any code.

## I1 — No platform credentials are requested, transmitted, or stored

No login flow, credential field, token store, or OAuth client for any delivery
platform exists anywhere in this repository. `db/schema.sql` has no credential
column on any table. Platform names appear only as enum labels on the driver's
own observations, and in `packageNames` on the capture config.

The one secret in the system is BLACKTOP's own Anthropic API key. It lives in
`server/`, never in the APK — which is the entire reason the vision read is an
HTTP endpoint rather than an on-device SDK call. `BuildConfig.EXTRACT_ENDPOINT`
holds a URL and nothing else, and is empty unless a build sets it.

## I2 — No network requests to platform servers

Grep-clean: no code references a doordash, uber, or grubhub domain or endpoint.
Those package names appear once, in `accessibility_service_config.xml`, to
*restrict* what the reader is allowed to see.

The app makes exactly one kind of outbound request: `POST` to
`BuildConfig.EXTRACT_ENDPOINT`, BLACKTOP's own vision endpoint, and only when
the driver has enabled the fallback. That endpoint calls the Anthropic API and
nothing else. All platform data enters as text already rendered on this
driver's screen, or as pixels from a capture of it.

`android/domain/` has no Android imports and performs no I/O of any kind — a
module boundary the build enforces. `core/blacktop/` is stdlib-only with no
HTTP client imported anywhere.

## I3 — No synthetic input events

This is the invariant most worth checking, because Android is a platform where
violating it is possible.

`OfferCaptureService` overrides `onAccessibilityEvent` to *read* node text and
nothing else. It never calls `performAction`, `dispatchGesture`, or
`performGlobalAction`. More usefully than that promise:
`accessibility_service_config.xml` declares `canRetrieveWindowContent` and no
gesture capability at all, so the platform would refuse an injection attempt
even if one were coded. The invariant is enforced in the manifest, where a
reviewer can confirm it without reading a line of Kotlin.

The HUD sets `FLAG_NOT_TOUCHABLE`. Every tap passes straight through to the app
underneath, so the overlay cannot intercept input and cannot be mistaken for
the accept button it sits above.

`Verdict` is a value object that gets rendered and spoken; no code path
consumes it to trigger an action. The accept and decline controls record what
the driver *already did* in the delivery app — inputs to the learning loop, not
outputs to the platform.

## I4 — Advisory only

Every output is a recommendation displayed to a human who then acts. The UI
states this on screen. `VerdictEngine.evaluate` returns a colour, a number, and
a spoken line; the driver taps in DoorDash.

The confidence gate matters here too. Below 0.75 extraction confidence the
engine returns `manual_fallback` with **no** projected hourly attached — not a
low-confidence number, no number. A fabricated distance produces a confident
wrong verdict, which is worse for a driver than no verdict, so a missing payout
produces no offer at all rather than an offer with a zero in it.

## I5 — All learned data is the driver's own observation of their own work

Every model in the system trains on the driver's own completed deliveries:

- **F2 tip estimator** — the label is the driver's actual payout minus what
  their card displayed.
- **F4 merchant wait** — the driver's own dwell time at that location.
- **F6 reservation rate** — the driver's own offers, declines included, and
  their own session timeline (which is what makes the λ correction legitimate:
  busy intervals come from the driver's clock, not the platform's).
- **Display cap detection** — counting repeats in the driver's own offer
  history. This is the highest-signal feature in the tip model and it requires
  no privileged access whatsoever, which is exactly why it cannot be taken
  away.

Storage is app-private newline-delimited JSON, and `data_extraction_rules.xml`
excludes it from both cloud backup and device transfer — the offer log is the
driver's business record, and it stays on the device they earned it on.
`OfferLog.exportJson` lets them take it with them. Nothing syncs anywhere.
`db/schema.sql` sets row-level security keyed to `driver_id` on every
driver-scoped table for when it does.

Two rules in the schema are enforced by the database rather than by convention,
because a client one typo away from leaking someone's door code is not a
sufficient barrier: a trigger rejects `buildings.shareable` on single-family
residences and on any row still carrying a gate code, and the
`shared_buildings` view omits the gate-code column entirely.

## I6 — Manual-entry fallback ships in v1 and works standalone

Held. `manualOffer()` in `android/domain/.../OfferParser.kt` builds a fully
trusted offer from three typed fields, and the form sits on the main screen
rather than behind a settings menu.

This is the hedge for the entire product, so it is deliberately prominent: the
consent screen points at it for drivers who would rather not enable screen
reading at all, and it works with zero permissions granted — no accessibility,
no overlay, no capture, no network.

## What a reviewer should check

- No credential field, token store, or platform login appears anywhere.
- `accessibility_service_config.xml` still declares no gesture capability, and
  `packageNames` still pins the reader to delivery apps.
- The HUD window still sets `FLAG_NOT_TOUCHABLE`.
- No `performAction` / `dispatchGesture` / `performGlobalAction` call exists.
- No platform domain is referenced in any request.
- `android/domain/` and `core/blacktop/` remain I/O-free.
- No code path acts on a `Verdict` other than rendering or speaking it.
- The confidence gate still refuses to emit a number below threshold.
- No API key appears anywhere under `android/`.
