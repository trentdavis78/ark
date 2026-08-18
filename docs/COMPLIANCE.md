# BLACKTOP — Compliance: the invariants and how the code enforces them

PRD §5.1 defines six non-negotiable invariants. This document states, for each, where and
how this codebase enforces it. Reviewers should treat any diff that weakens one of these
as release-blocking.

## I1 — No platform credentials are requested, transmitted, or stored

- There is no login flow, credential field, token store, or OAuth client for any delivery
  platform anywhere in `android/` or `backend/`.
- The Room schema (`android/.../data/`) and the SQL migration
  (`backend/supabase/migrations/0001_init.sql`) contain no credential columns.
- Supabase auth authenticates the **driver to BLACKTOP's own backend** only.

## I2 — No network requests to platform servers

- Grep-clean guarantee: no code references doordash/uber/grubhub domains or endpoints.
  Platform names appear only as enum labels on the driver's own observations
  (`Platform.DOORDASH` etc.).
- All platform data enters exclusively via what is already rendered on the driver's own
  screen (accessibility node text / OCR text) or via manual entry.
- The core engine (`core/blacktop/`) performs no I/O at all: stdlib-only, no sockets, no
  HTTP client imports.

## I3 — No synthetic input events

- `OfferCaptureAccessibilityService` is **read-only**: it overrides `onAccessibilityEvent`
  to traverse node text and never calls `performAction`, `dispatchGesture`,
  `performGlobalAction`, or any input-injection API. The service config requests
  `canRetrieveWindowContent` only — no gesture capability flags.
- There is no code path from a `Verdict` to any injected interaction. The verdict flows to
  `overlay/VerdictRenderer` and `overlay/TtsAnnouncer` and terminates there.

## I4 — Advisory only

- `Verdict` is a pure value object (`core/blacktop/models.py`); the engine's public API
  returns it and nothing consumes it except rendering/audio.
- UX shows the number **and** the threshold it was compared against (PRD §9.5) so the human
  decides with context; BLACKTOP never acts.

## I5 — All learned data is the driver's own observation of their own work

- Every learned store (merchant waits, building intel, zone stats, tip labels) is written
  only from the driver's own sessions. RLS in `0001_init.sql` keys every table to
  `driver_id = auth.uid()`, so one driver can never read another's rows.
- Gate codes are encrypted at rest (`gate_code_encrypted`; the migration documents pgsodium
  usage). `buildings.shareable` defaults `false` and may only flip true for commercial /
  multi-unit properties on explicit opt-in — enforced by a CHECK + trigger in the
  migration and by `building_intel.py::BuildingIntel.set_shareable`, which refuses
  single-family residences.

## I6 — Manual-entry fallback mode ships in v1 and works standalone

- `core/blacktop/parser.py` exposes `manual_offer(...)` — a first-class structured entry
  path producing the same `ParsedOffer` type at confidence 1.0.
- `android/ui/ManualEntryScreen.kt` is reachable without enabling any capture permission;
  the app is fully functional (verdicts, mileage, session tracking) with screen reading
  disabled.
- Confidence gating: `verdict.py` refuses to emit a verdict when parse confidence is below
  `MIN_PARSE_CONFIDENCE` and instead returns a `MANUAL_FALLBACK` verdict directing the
  driver to manual mode — a wrong verdict is worse than no verdict (PRD §11, parse-risk
  row). Parse failures are telemetry: the parser returns machine-readable failure reasons.

## Informed consent (PRD §11, row 1)

- `android/ui/ConsentScreen.kt` is a plain-language, explicit consent gate shown **before**
  the accessibility service or MediaProjection can be enabled. It states the gray-zone risk
  verbatim and offers manual mode as the alternative. Enabling capture without passing this
  screen is not possible in the UI flow.

## Guardrails in the optimizer

- `reservation.py` enforces the policy-constraint layer: completion-rate floor ≥95%
  (never recommend an accept that risks an unassign), lateness avoidance bounds on
  arrival-timing advice (`merchant_oracle.py`), and the sanity band clamp
  ($0.30–$0.85 per projected minute) so upstream failures cannot produce absurd advice.
