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

The PRD accepts one residual risk: on-device reading of the offer card via OCR
or accessibility services is a gray zone. It touches no platform server and
uses no credentials, but it is not blessed by any platform's terms, and PRD
§5.1 calls invariant I6 — the manual fallback — "the risk hedge for the entire
product."

**This build does not take that risk.** There is no accessibility service, no
screen reading, and no overlay, because a PWA cannot do any of them. Input is a
screenshot the driver took of their own screen and chose to share. The gray
zone is not mitigated here; it is absent.

That is not a free win. It costs the sub-400ms verdict and the zero-touch
shift, both of which the PRD treats as core. See
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## I1 — No platform credentials are requested, transmitted, or stored

No login flow, credential field, token store, or OAuth client for any delivery
platform exists anywhere in this repository. `db/schema.sql` has no credential
column on any table. Platform names appear only as enum labels on the driver's
own observations (`"doordash"`, `"uber_eats"`, `"grubhub"`).

The one secret in the system is BLACKTOP's own Anthropic API key. It lives
server-side in `web/src/server/extract.ts` and never reaches the browser; the
build is checked to confirm neither the SDK nor key material appears in the
client bundle.

## I2 — No network requests to platform servers

Grep-clean: no code references a doordash, uber, or grubhub domain or endpoint.

The app makes exactly two kinds of outbound request — its own origin for the
app shell, and `POST /api/extract` to its own vision endpoint. That endpoint
calls the Anthropic API and nothing else. All platform data enters as pixels in
a screenshot the driver already had on their device.

`web/src/domain/` performs no I/O of any kind: no `fetch`, no storage, no DOM.
`core/blacktop/` is stdlib-only with no HTTP client imported anywhere.

## I3 — No synthetic input events

There is nothing in this codebase that can tap anything. A web page cannot
dispatch input to another application, and BLACKTOP does not attempt to
automate its own UI either. `Verdict` is a value object that gets rendered and
spoken; no code path consumes it to trigger an action.

The accept and decline buttons in `web/src/app/main.ts` record what the driver
*already did* in the platform app. They are inputs to the learning loop, not
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

Storage is local (IndexedDB). Nothing syncs anywhere. `db/schema.sql` sets
row-level security keyed to `driver_id` on every driver-scoped table for when
it does.

Two rules in the schema are enforced by the database rather than by convention,
because a client one typo away from leaking someone's door code is not a
sufficient barrier: a trigger rejects `buildings.shareable` on single-family
residences and on any row still carrying a gate code, and the
`shared_buildings` view omits the gate-code column entirely.

## I6 — Manual-entry fallback ships in v1 and works standalone

Partially held, and the gap is worth stating plainly.

The engine accepts a hand-built `Offer` — `makeOffer` in
`web/src/domain/models.ts` — and the whole decision path runs on it with no
vision involved. The Python core exposes `manual_offer()` for the same purpose
and it is tested.

But the shipped UI has no manual-entry form. If the vision endpoint is
unreachable, the driver currently gets a clear failure and no way to score the
offer by hand. Given that this build's *only* input path is vision, that makes
the fallback more important here than the PRD envisaged, not less. It is the
first thing to add.

## What a reviewer should check

- No credential field, token store, or platform login appears anywhere.
- No platform domain is referenced in any request.
- `web/src/domain/` and `core/blacktop/` remain I/O-free.
- No code path acts on a `Verdict` other than rendering or speaking it.
- The confidence gate still refuses to emit a number below threshold.
- The API key still does not appear in the client bundle.
